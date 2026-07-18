from __future__ import annotations

from typing import Any, Dict, List

# 抖音图文/笔记常见 aweme_type（实测 68 为图集）
IMAGE_AWEME_TYPES = {2, 68, 150}


def extract_image_urls(aweme_detail: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    urls.extend(_urls_from_image_list(aweme_detail.get("images", [])))
    image_post_info = aweme_detail.get("image_post_info", {})
    if not isinstance(image_post_info, dict):
        image_post_info = {}
    urls.extend(_urls_from_display_images(image_post_info.get("images", [])))
    # 去重保序
    seen = set()
    unique: List[str] = []
    for url in urls:
        text = str(url or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def is_image_post(aweme_detail: Dict[str, Any]) -> bool:
    """Whether this aweme should be treated as image/note rather than video."""
    if extract_image_urls(aweme_detail):
        return True
    raw_type = aweme_detail.get("aweme_type")
    try:
        aweme_type = int(raw_type)
    except (TypeError, ValueError):
        aweme_type = None
    if aweme_type in IMAGE_AWEME_TYPES:
        return True
    # play_addr.uri 是音乐 CDN 时，多半是图文误带 video 壳
    video = aweme_detail.get("video")
    if isinstance(video, dict):
        play_addr = video.get("play_addr")
        if isinstance(play_addr, dict):
            uri = str(play_addr.get("uri") or "")
            if "ies-music" in uri or uri.endswith(".mp3"):
                return True
    return False


def _urls_from_image_list(image_items: Any) -> List[str]:
    if not isinstance(image_items, list):
        return []
    urls: List[str] = []
    for image_item in image_items:
        if not isinstance(image_item, dict):
            continue
        url_list = image_item.get("url_list", [])
        if isinstance(url_list, list) and url_list:
            urls.append(url_list[0])
    return urls


def _urls_from_display_images(image_post_items: Any) -> List[str]:
    if not isinstance(image_post_items, list):
        return []
    urls: List[str] = []
    for image_item in image_post_items:
        if not isinstance(image_item, dict):
            continue
        display_image = image_item.get("display_image", {})
        if not isinstance(display_image, dict):
            continue
        url_list = display_image.get("url_list", [])
        if isinstance(url_list, list) and url_list:
            urls.append(url_list[0])
    return urls
