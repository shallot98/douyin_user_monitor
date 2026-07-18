import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
import httpx

from douyin_user_monitor.monitor.media_urls import extract_image_urls, is_image_post
from douyin_user_monitor.monitor.original_play import (
    extract_video_id,
    resolve_original_play_url,
)
from douyin_user_monitor.monitor.video_source import (
    VideoSource,
    build_original_video_source,
    extract_video_source,
    select_clearest_video_source,
    select_highest_bit_rate_source,
)

DOWNLOAD_TIMEOUT_SECONDS = 30
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 300
MAX_DESC_LENGTH = 48
MAX_USER_FOLDER_LENGTH = 64
IMAGE_ROOT_FOLDER = "图片"
MEDIA_TYPE_VIDEO = "video"
MEDIA_TYPE_IMAGE = "image"

INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def sanitize_text(value: str, default_value: str, *, max_length: int = MAX_DESC_LENGTH) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", value or "").strip(" ._")
    if not cleaned:
        return default_value
    return cleaned[:max_length]


class AwemeAssetDownloader:
    def __init__(self, download_root: Path):
        self._download_root = download_root
        self._download_root.mkdir(parents=True, exist_ok=True)

    async def download_aweme_assets(
        self,
        *,
        aweme_id: str,
        sec_user_id: str,
        user_nickname: str,
        aweme_detail: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Dict[str, Any]:
        user_folder = self._resolve_user_folder(sec_user_id=sec_user_id, user_nickname=user_nickname)
        filename = self._build_aweme_filename(aweme_detail)
        # 图文作品有时也会带 video/音乐 uri；优先按图片处理，禁止走原画 play。
        image_urls = self._extract_image_urls(aweme_detail)
        if image_urls or is_image_post(aweme_detail):
            if not image_urls:
                raise ValueError(f"作品 {aweme_id} 判定为图文但未解析到图片链接")
            image_folder = user_folder / IMAGE_ROOT_FOLDER / filename
            image_result = await self._download_images(image_urls, headers, image_folder)
            return self._build_download_result(
                media_type=MEDIA_TYPE_IMAGE,
                files=image_result["files"],
                downloaded_file_count=image_result["downloaded_file_count"],
                existing_file_count=image_result["existing_file_count"],
                image_count=len(image_urls),
            )
        video_source = await self._resolve_video_source(aweme_detail, headers)
        if video_source:
            video_file = user_folder / f"{filename}.mp4"
            is_downloaded = await self._download_video(video_source.url, headers, video_file)
            return self._build_download_result(
                media_type=MEDIA_TYPE_VIDEO,
                files=[video_file],
                downloaded_file_count=1 if is_downloaded else 0,
                existing_file_count=0 if is_downloaded else 1,
                video_source=video_source,
            )
        raise ValueError(f"作品 {aweme_id} 未找到可下载的视频或图片链接")

    async def _resolve_video_source(
        self,
        aweme_detail: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Optional[VideoSource]:
        original = await self._try_original_source(aweme_detail, headers)
        if original is not None:
            return original
        return self._extract_video_source(aweme_detail)

    async def _try_original_source(
        self,
        aweme_detail: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Optional[VideoSource]:
        video_id = extract_video_id(aweme_detail)
        if not video_id:
            return None
        final_url = await resolve_original_play_url(video_id, headers)
        if not final_url:
            return None
        width, height = _play_addr_dimensions(aweme_detail)
        return build_original_video_source(final_url, width=width, height=height)

    async def _download_video(self, video_url: str, headers: Dict[str, str], file_path: Path) -> bool:
        if file_path.exists():
            return False
        timeout = httpx.Timeout(VIDEO_DOWNLOAD_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", video_url, headers=headers) as response:
                response.raise_for_status()
                async with aiofiles.open(file_path, "wb") as output_file:
                    async for chunk in response.aiter_bytes():
                        await output_file.write(chunk)
        return True

    async def _download_images(
        self,
        image_urls: List[str],
        headers: Dict[str, str],
        image_folder: Path,
    ) -> Dict[str, Any]:
        image_folder.mkdir(parents=True, exist_ok=True)
        files: List[Path] = []
        downloaded_file_count = 0
        existing_file_count = 0
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as client:
            for index, image_url in enumerate(image_urls, start=1):
                image_file = image_folder / f"{index}.jpg"
                files.append(image_file)
                if image_file.exists():
                    existing_file_count += 1
                    continue
                response = await client.get(image_url, headers=headers)
                response.raise_for_status()
                async with aiofiles.open(image_file, "wb") as output_file:
                    await output_file.write(response.content)
                downloaded_file_count += 1
        return {
            "files": files,
            "downloaded_file_count": downloaded_file_count,
            "existing_file_count": existing_file_count,
        }

    def _resolve_user_folder(self, *, sec_user_id: str, user_nickname: str) -> Path:
        folder_name = self._build_user_folder_name(sec_user_id=sec_user_id, user_nickname=user_nickname)
        user_folder = self._download_root / folder_name
        user_folder.mkdir(parents=True, exist_ok=True)
        return user_folder

    def _build_user_folder_name(self, *, sec_user_id: str, user_nickname: str) -> str:
        if not user_nickname:
            return sanitize_text(sec_user_id, "unknown_user", max_length=MAX_USER_FOLDER_LENGTH)
        return sanitize_text(user_nickname, "unknown_user", max_length=MAX_USER_FOLDER_LENGTH)

    def _extract_video_url(self, aweme_detail: Dict[str, Any]) -> Optional[str]:
        video_source = self._extract_video_source(aweme_detail)
        return video_source.url if video_source else None

    def _extract_video_source(self, aweme_detail: Dict[str, Any]) -> Optional[VideoSource]:
        return extract_video_source(aweme_detail)

    def _select_clearest_video_source(self, video_data: Dict[str, Any]) -> Optional[VideoSource]:
        return select_clearest_video_source(video_data)

    def _select_highest_bit_rate_source(self, video_data: Dict[str, Any]) -> Optional[VideoSource]:
        return select_highest_bit_rate_source(video_data)

    def _extract_image_urls(self, aweme_detail: Dict[str, Any]) -> List[str]:
        return extract_image_urls(aweme_detail)

    def _build_aweme_filename(self, aweme_detail: Dict[str, Any]) -> str:
        create_time = int(aweme_detail.get("create_time", 0))
        if create_time > 0:
            time_str = datetime.fromtimestamp(create_time).strftime("%Y%m%d_%H%M%S")
        else:
            time_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        desc = sanitize_text(aweme_detail.get("desc", ""), "aweme")
        return f"{time_str}_{desc}"

    def _build_download_result(
        self,
        *,
        media_type: str,
        files: List[Path],
        downloaded_file_count: int,
        existing_file_count: int,
        image_count: int = 0,
        video_source: Optional[VideoSource] = None,
    ) -> Dict[str, Any]:
        result = {
            "media_type": media_type,
            "files": [self._relative_record_path(path) for path in files],
            "downloaded_file_count": downloaded_file_count,
            "existing_file_count": existing_file_count,
            "image_count": image_count,
            "total_size_bytes": self._calculate_total_size(files),
        }
        if video_source:
            result["video_source"] = video_source.to_record()
        return result

    def _relative_record_path(self, file_path: Path) -> str:
        try:
            return str(file_path.relative_to(self._download_root))
        except ValueError:
            return str(file_path)

    def _calculate_total_size(self, files: List[Path]) -> int:
        total = 0
        for path in files:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total


def _play_addr_dimensions(aweme_detail: Dict[str, Any]) -> tuple[int, int]:
    video = aweme_detail.get("video")
    if not isinstance(video, dict):
        return 0, 0
    play_addr = video.get("play_addr")
    if not isinstance(play_addr, dict):
        return 0, 0
    try:
        width = int(play_addr.get("width") or 0)
    except (TypeError, ValueError):
        width = 0
    try:
        height = int(play_addr.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    return width, height
