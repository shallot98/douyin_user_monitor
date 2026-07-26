import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

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
)

DOWNLOAD_TIMEOUT_SECONDS = 30
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 300
MAX_DESC_LENGTH = 48
MAX_USER_FOLDER_LENGTH = 64
IMAGE_ROOT_FOLDER = "图片"
MEDIA_TYPE_VIDEO = "video"
MEDIA_TYPE_IMAGE = "image"

INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')

MAX_DOWNLOAD_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.5
# 只对连接类/瞬时故障重试；4xx/5xx 的 HTTPStatusError 与字节校验 ValueError 直接抛出，不重试。
RETRYABLE_DOWNLOAD_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


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
        filename = self._build_aweme_filename(aweme_detail, aweme_id)
        # 图文作品有时也会带 video/音乐 uri；优先按图片处理，禁止走原画 play。
        image_urls = extract_image_urls(aweme_detail)
        # 作品级共享 client：原画探测与视频/图片下载复用同一连接池，省去重复 TCP/TLS 握手。
        async with self._create_shared_client() as client:
            if image_urls or is_image_post(aweme_detail):
                if not image_urls:
                    raise ValueError(f"作品 {aweme_id} 判定为图文但未解析到图片链接")
                image_folder = user_folder / IMAGE_ROOT_FOLDER / filename
                image_result = await self._download_images(image_urls, headers, image_folder, client=client)
                return self._build_download_result(
                    media_type=MEDIA_TYPE_IMAGE,
                    files=image_result["files"],
                    downloaded_file_count=image_result["downloaded_file_count"],
                    existing_file_count=image_result["existing_file_count"],
                    image_count=len(image_urls),
                )
            video_source = await self._resolve_video_source(aweme_detail, headers, client=client)
            if video_source:
                video_file = user_folder / f"{filename}.mp4"
                is_downloaded = await self._download_video(video_source.url, headers, video_file, client=client)
                return self._build_download_result(
                    media_type=MEDIA_TYPE_VIDEO,
                    files=[video_file],
                    downloaded_file_count=1 if is_downloaded else 0,
                    existing_file_count=0 if is_downloaded else 1,
                    video_source=video_source,
                )
            raise ValueError(f"作品 {aweme_id} 未找到可下载的视频或图片链接")

    def _create_shared_client(self) -> httpx.AsyncClient:
        # 默认超时按图片档；视频下载在请求级覆盖为更长的 VIDEO_DOWNLOAD_TIMEOUT_SECONDS。
        return httpx.AsyncClient(
            timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS),
            follow_redirects=True,
        )

    async def _run_with_retry(self, make_attempt: Callable[[], Awaitable[Any]]) -> Any:
        last_error: Optional[BaseException] = None
        for attempt_index in range(MAX_DOWNLOAD_ATTEMPTS):
            try:
                return await make_attempt()
            except RETRYABLE_DOWNLOAD_ERRORS as error:
                last_error = error
                if attempt_index + 1 >= MAX_DOWNLOAD_ATTEMPTS:
                    break
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt_index + 1))
        if last_error is None:
            raise RuntimeError("下载重试未产生任何结果（MAX_DOWNLOAD_ATTEMPTS 配置无效）")
        raise last_error

    async def _resolve_video_source(
        self,
        aweme_detail: Dict[str, Any],
        headers: Dict[str, str],
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Optional[VideoSource]:
        original = await self._try_original_source(aweme_detail, headers, client=client)
        if original is not None:
            return original
        return self._extract_video_source(aweme_detail)

    async def _try_original_source(
        self,
        aweme_detail: Dict[str, Any],
        headers: Dict[str, str],
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Optional[VideoSource]:
        video_id = extract_video_id(aweme_detail)
        if not video_id:
            return None
        final_url = await resolve_original_play_url(video_id, headers, client=client)
        if not final_url:
            return None
        width, height = _play_addr_dimensions(aweme_detail)
        return build_original_video_source(final_url, width=width, height=height)

    async def _download_video(
        self,
        video_url: str,
        headers: Dict[str, str],
        file_path: Path,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> bool:
        if file_path.exists():
            return False
        part_path = _part_path(file_path)
        timeout = httpx.Timeout(VIDEO_DOWNLOAD_TIMEOUT_SECONDS)

        if client is None:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as owned:
                await self._run_with_retry(
                    lambda: self._stream_video_once(owned, video_url, headers, file_path, part_path, timeout)
                )
        else:
            await self._run_with_retry(
                lambda: self._stream_video_once(client, video_url, headers, file_path, part_path, timeout)
            )
        return True

    async def _stream_video_once(
        self,
        client: httpx.AsyncClient,
        video_url: str,
        headers: Dict[str, str],
        file_path: Path,
        part_path: Path,
        timeout: httpx.Timeout,
    ) -> None:
        try:
            async with client.stream("GET", video_url, headers=headers, timeout=timeout) as response:
                response.raise_for_status()
                expected_size = _content_length(response)
                written_size = 0
                async with aiofiles.open(part_path, "wb") as output_file:
                    async for chunk in response.aiter_bytes():
                        await output_file.write(chunk)
                        written_size += len(chunk)
                _verify_written_size(expected_size, written_size, video_url)
            os.replace(part_path, file_path)
        finally:
            # 每次尝试结束清理 .part：失败重试前从干净状态开始，成功后 replace 已移走原子写文件。
            part_path.unlink(missing_ok=True)

    async def _download_images(
        self,
        image_urls: List[str],
        headers: Dict[str, str],
        image_folder: Path,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Dict[str, Any]:
        image_folder.mkdir(parents=True, exist_ok=True)
        if client is None:
            async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as owned:
                return await self._download_images_with_client(owned, image_urls, headers, image_folder)
        return await self._download_images_with_client(client, image_urls, headers, image_folder)

    async def _download_images_with_client(
        self,
        client: httpx.AsyncClient,
        image_urls: List[str],
        headers: Dict[str, str],
        image_folder: Path,
    ) -> Dict[str, Any]:
        files: List[Path] = []
        downloaded_file_count = 0
        existing_file_count = 0
        for index, image_url in enumerate(image_urls, start=1):
            image_file = image_folder / f"{index}.jpg"
            files.append(image_file)
            if image_file.exists():
                existing_file_count += 1
                continue
            await self._run_with_retry(
                lambda url=image_url, target=image_file: self._download_image_once(client, url, headers, target)
            )
            downloaded_file_count += 1
        return {
            "files": files,
            "downloaded_file_count": downloaded_file_count,
            "existing_file_count": existing_file_count,
        }

    async def _download_image_once(
        self,
        client: httpx.AsyncClient,
        image_url: str,
        headers: Dict[str, str],
        image_file: Path,
    ) -> None:
        part_path = _part_path(image_file)
        try:
            response = await client.get(image_url, headers=headers, timeout=DOWNLOAD_TIMEOUT_SECONDS)
            response.raise_for_status()
            content = response.content
            _verify_written_size(_content_length(response), len(content), image_url)
            async with aiofiles.open(part_path, "wb") as output_file:
                await output_file.write(content)
            os.replace(part_path, image_file)
        finally:
            part_path.unlink(missing_ok=True)

    def _resolve_user_folder(self, *, sec_user_id: str, user_nickname: str) -> Path:
        folder_name = self._build_user_folder_name(sec_user_id=sec_user_id, user_nickname=user_nickname)
        user_folder = self._download_root / folder_name
        user_folder.mkdir(parents=True, exist_ok=True)
        return user_folder

    def _build_user_folder_name(self, *, sec_user_id: str, user_nickname: str) -> str:
        if not user_nickname:
            return sanitize_text(sec_user_id, "unknown_user", max_length=MAX_USER_FOLDER_LENGTH)
        return sanitize_text(user_nickname, "unknown_user", max_length=MAX_USER_FOLDER_LENGTH)

    def _extract_video_source(self, aweme_detail: Dict[str, Any]) -> Optional[VideoSource]:
        return extract_video_source(aweme_detail)

    def _build_aweme_filename(self, aweme_detail: Dict[str, Any], aweme_id: str = "") -> str:
        # 文件名必须可复现且全局唯一：时间可省略，aweme_id 参与命名去重同秒同文案作品。
        aweme_id = str(aweme_detail.get("aweme_id") or aweme_id or "")
        try:
            create_time = int(aweme_detail.get("create_time") or 0)
        except (TypeError, ValueError):
            create_time = 0
        parts: List[str] = []
        if create_time > 0:
            parts.append(datetime.fromtimestamp(create_time).strftime("%Y%m%d_%H%M%S"))
        if aweme_id:
            parts.append(aweme_id)
        parts.append(sanitize_text(aweme_detail.get("desc", ""), "aweme"))
        return "_".join(parts)

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


def _part_path(file_path: Path) -> Path:
    return file_path.with_name(f"{file_path.name}.part")


def _content_length(response: httpx.Response) -> Optional[int]:
    # 响应经内容编码（gzip/br/deflate）时 Content-Length 是压缩体大小，httpx 交付的是解压后字节，
    # 二者不可比；此时返回 None 让完整性校验跳过，避免把正常下载误判为字节数不一致。
    encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if encoding and encoding != "identity":
        return None
    raw_length = response.headers.get("Content-Length")
    if raw_length is None:
        return None
    try:
        return int(raw_length)
    except ValueError:
        return None


def _verify_written_size(expected_size: Optional[int], actual_size: int, url: str) -> None:
    if expected_size is not None and expected_size != actual_size:
        raise ValueError(f"下载字节数不一致（预期 {expected_size}，实际 {actual_size}）：{url}")


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
