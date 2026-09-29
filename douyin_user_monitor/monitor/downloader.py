import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiofiles
import httpx

from douyin_user_monitor.monitor.cover_image import normalize_cover
from douyin_user_monitor.monitor.media_urls import extract_image_urls, is_image_post
from douyin_user_monitor.monitor.original_play import (
    extract_video_id,
    resolve_original_play_url,
)
from douyin_user_monitor.monitor.playback_media import probe_playback_media
from douyin_user_monitor.monitor.playback_prepare import (
    ArchiveMediaInspectionError,
    CoverPreparer,
    PlaybackPreparer,
    PlaybackRequest,
    PreparedPlayback,
    prepare_cover,
    prepare_playback,
)
from douyin_user_monitor.monitor.video_source import (
    VideoSource,
    build_original_video_source,
    extract_cover_url,
    extract_video_source,
    select_fast_play_source,
)

DOWNLOAD_TIMEOUT_SECONDS = 30
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 300
MAX_DESC_LENGTH = 48
MAX_USER_FOLDER_LENGTH = 64
IMAGE_ROOT_FOLDER = "图片"
# 网页展示用附件：与归档原画同目录，靠后缀区分
FAST_PLAY_SUFFIX = ".fast.mp4"
COVER_SUFFIX = ".cover.jpg"
COVER_FILE_NAME = "cover.jpg"
MEDIA_TYPE_VIDEO = "video"
MEDIA_TYPE_IMAGE = "image"

logger = logging.getLogger(__name__)

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


async def _has_valid_image(target: Path) -> bool:
    if not target.exists():
        return False
    try:
        await asyncio.to_thread(normalize_cover, target)
    except (OSError, SyntaxError, ValueError):
        logger.warning("Existing image is invalid; preparing it again: %s", target, exc_info=True)
        return False
    return True


class AwemeAssetDownloader:
    def __init__(
        self,
        download_root: Path,
        *,
        playback_preparer: PlaybackPreparer = prepare_playback,
        cover_preparer: CoverPreparer = prepare_cover,
    ):
        self._download_root = download_root
        self._download_root.mkdir(parents=True, exist_ok=True)
        self._prepare_playback = playback_preparer
        self._prepare_cover = cover_preparer
        self._playback_preparation_lock = asyncio.Lock()

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
        image_urls = extract_image_urls(aweme_detail)
        async with self._create_shared_client() as client:
            if image_urls or is_image_post(aweme_detail):
                result = await self._download_image_asset(image_urls, aweme_detail, headers, user_folder, filename, client)
            else:
                result = await self._download_video_asset(aweme_id, aweme_detail, headers, user_folder, filename, client)
        return result

    async def _download_image_asset(self, image_urls, aweme_detail, headers, user_folder, filename, client):
        if not image_urls:
            raise ValueError(f"图文作品 {filename} 未解析到图片链接")
        folder = user_folder / IMAGE_ROOT_FOLDER / filename
        images = await self._download_images(image_urls, headers, folder, client=client)
        cover = await self._download_cover(aweme_detail, headers, folder / COVER_FILE_NAME, client=client,
                                           fallback_source=images["files"][0])
        files = [*images["files"], cover["file"]]
        return self._build_download_result(media_type=MEDIA_TYPE_IMAGE, files=files,
            downloaded_file_count=images["downloaded_file_count"] + cover["downloaded"],
            existing_file_count=images["existing_file_count"] + cover["existing"], image_count=len(image_urls),
            cover_file=cover["file"])

    async def _download_video_asset(self, aweme_id, aweme_detail, headers, user_folder, filename, client):
        source = await self._resolve_video_source(aweme_detail, headers, client=client)
        if not source:
            raise ValueError(f"作品 {aweme_id} 未找到可下载的视频链接")
        video_file = user_folder / f"{filename}.mp4"
        downloaded = await self._download_video(source.url, headers, video_file, client=client)
        try:
            extras = await self._download_playback_extras(aweme_detail, headers, user_folder=user_folder,
                                                          filename=filename, archive_source=source, client=client)
        except ArchiveMediaInspectionError:
            if downloaded:
                raise
            logger.warning("Cached archive inspection failed; downloading its source again: %s", video_file,
                           exc_info=True)
            await self._refresh_rejected_archive(source.url, headers, video_file, client=client)
            downloaded = True
            extras = await self._download_playback_extras(aweme_detail, headers, user_folder=user_folder,
                                                          filename=filename, archive_source=source, client=client)
        return self._build_download_result(media_type=MEDIA_TYPE_VIDEO, files=[video_file, *extras["files"]],
            downloaded_file_count=int(downloaded) + extras["downloaded_file_count"],
            existing_file_count=int(not downloaded) + extras["existing_file_count"], video_source=source,
            video_file=video_file, fast_play_file=extras["fast_play_file"], cover_file=extras["cover_file"],
            fast_play_source=extras["fast_play_source"])

    async def _refresh_rejected_archive(self, url, headers, target, *, client):
        temporary = target.with_name(f"{target.name}.retry.part")
        try:
            await self._download_video(url, headers, temporary, client=client)
            await probe_playback_media(temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

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

    async def _download_playback_extras(
        self,
        aweme_detail: Dict[str, Any],
        headers: Dict[str, str],
        *,
        user_folder: Path,
        filename: str,
        archive_source: Optional[VideoSource],
        client: Optional[httpx.AsyncClient],
    ) -> Dict[str, Any]:
        """Publish verified playback and cover roles; failures leave the archive for retry."""
        original = user_folder / f"{filename}.mp4"
        prepared = await self._download_fast_play(
            aweme_detail, headers, archive_path=original, archive_source=archive_source, client=client
        )
        separate_playback = prepared.path != original
        files = [prepared.path] if separate_playback else []
        cover = await self._download_cover(
            aweme_detail, headers, user_folder / f"{filename}{COVER_SUFFIX}", client=client,
            fallback_source=prepared.path,
        )
        files.append(cover["file"])
        return {
            "files": files,
            "downloaded_file_count": int(separate_playback and prepared.created) + cover["downloaded"],
            "existing_file_count": int(separate_playback and not prepared.created) + cover["existing"],
            "fast_play_file": prepared.path,
            "fast_play_source": prepared.to_video_source(),
            "cover_file": cover["file"],
        }

    async def _download_fast_play(
        self,
        aweme_detail: Dict[str, Any],
        headers: Dict[str, str],
        *,
        archive_path: Path,
        archive_source: Optional[VideoSource],
        client: Optional[httpx.AsyncClient],
    ) -> PreparedPlayback:
        target = archive_path.with_suffix(FAST_PLAY_SUFFIX)
        candidate_path = target.with_name(f"{target.name}.source.part")
        video_data = aweme_detail.get("video")
        source = select_fast_play_source(video_data) if isinstance(video_data, dict) else None
        if source and archive_source and source.url == archive_source.url:
            source = None
        candidate = None
        try:
            if source and not target.exists():
                await self._download_video(source.url, headers, candidate_path, client=client)
                candidate = candidate_path
            request = PlaybackRequest(archive_path=archive_path, target_path=target, candidate_path=candidate)
            # One preparation per downloader keeps concurrent user syncs from saturating this host.
            async with self._playback_preparation_lock:
                return await self._prepare_playback(request)
        finally:
            candidate_path.unlink(missing_ok=True)

    async def _download_cover(
        self,
        aweme_detail: Dict[str, Any],
        headers: Dict[str, str],
        target: Path,
        *,
        client: Optional[httpx.AsyncClient],
        fallback_source: Optional[Path] = None,
    ) -> Dict[str, Any]:
        cover_url = extract_cover_url(aweme_detail)
        if await _has_valid_image(target):
            return {"file": target, "downloaded": 0, "existing": 1}
        target.parent.mkdir(parents=True, exist_ok=True)
        if not cover_url:
            if fallback_source is None:
                raise ValueError(f"作品封面缺少上游链接或可用媒体源：{target.name}")
            async with self._playback_preparation_lock:
                await self._prepare_cover(fallback_source, target)
            return {"file": target, "downloaded": 1, "existing": 0}
        if client is None:
            async with httpx.AsyncClient(
                timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
            ) as owned:
                await self._run_with_retry(
                    lambda: self._download_image_once(owned, cover_url, headers, target)
                )
        else:
            await self._run_with_retry(
                lambda: self._download_image_once(client, cover_url, headers, target)
            )
        return {"file": target, "downloaded": 1, "existing": 0}

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
            if await _has_valid_image(image_file):
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
        video_file: Optional[Path] = None,
        fast_play_file: Optional[Path] = None,
        cover_file: Optional[Path] = None,
        fast_play_source: Optional[VideoSource] = None,
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
        # 显式记录各角色文件，网页端不必靠文件名猜（老记录没有这些键，展示层回落 files）
        if video_file is not None:
            result["video_file"] = self._relative_record_path(video_file)
        if fast_play_file is not None:
            result["fast_play_file"] = self._relative_record_path(fast_play_file)
        if cover_file is not None:
            result["cover_file"] = self._relative_record_path(cover_file)
        if fast_play_source:
            result["fast_play_source"] = fast_play_source.to_record()
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
