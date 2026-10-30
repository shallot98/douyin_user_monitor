"""历史作品补打标签用的媒体来源：本机还在就读本机，否则从网盘（OpenList）读。

网盘读取不做 ffprobe 运动检测（要读完整个文件），只顺序读关键帧；封面优先用本机 CoverStore。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from douyin_user_monitor.monitor.content_filter import (
    ContentFilterError,
    LocalMediaLoader,
    PreparedMedia,
    extract_stream_keyframes,
    first_video_file,
    image_files,
    image_post_note,
    pick_evenly,
    shrink_image,
    shrink_image_bytes,
    video_note,
)

MAX_REMOTE_IMAGE_BYTES = 20 * 1024 * 1024
REMOTE_IMAGE_TIMEOUT_SECONDS = 60


class ArchiveMediaLoader:
    def __init__(self, download_root: Path, openlist, aliases, cover_store, *, transport=None):
        self._local = LocalMediaLoader(download_root)
        self._openlist = openlist
        self._aliases = aliases
        self._cover_store = cover_store
        self._transport = transport

    async def prepare(self, user: Dict[str, Any], record: Dict[str, Any], max_frames: int) -> PreparedMedia:
        if self._all_local(record):
            media = await self._local.prepare(user, record, max_frames)
            return media
        media_type = record.get("media_type")
        if media_type == "video":
            return await self._prepare_remote_video(user, record, max_frames)
        if media_type == "image":
            return await self._prepare_remote_images(user, record, max_frames)
        raise ContentFilterError(f"不支持的作品类型：{media_type!r}")

    def _all_local(self, record: Dict[str, Any]) -> bool:
        if record.get("media_type") == "video":
            return self._local.optional(first_video_file(record)) is not None
        names = image_files(record)
        return bool(names) and all(self._local.optional(name) is not None for name in names)

    async def _url(self, user: Dict[str, Any], record: Dict[str, Any], relative: str) -> str:
        verified = self._aliases.paths_for(user, str(record["aweme_id"]), relative)
        return await self._openlist.resolve_stream_url(relative, sec_user_id=user.get("sec_user_id"),
                                                       verified_paths=verified)

    async def _cover(self, user: Dict[str, Any], record: Dict[str, Any]) -> Optional[bytes]:
        local = self._local.optional(record.get("cover_file"))
        if local is not None:
            return await asyncio.to_thread(shrink_image, local)
        metadata = await asyncio.to_thread(self._cover_store.lookup, str(user["id"]), record)
        if metadata is None:
            return None
        return await asyncio.to_thread(shrink_image, metadata.path)

    async def _prepare_remote_video(self, user, record, max_frames) -> PreparedMedia:
        # 快播版是 720p h264 + faststart：顺序读更快、更省流量，截图够用
        relative = str(record.get("fast_play_file") or first_video_file(record))
        if not relative:
            raise ContentFilterError("记录缺少视频文件路径")
        images = await extract_stream_keyframes(await self._url(user, record, relative), max_frames)
        cover = await self._cover(user, record)
        if cover is not None:
            images.insert(0, cover)
        return PreparedMedia(images, video_note(cover is not None), cover_used=cover is not None, source="archive")

    async def _prepare_remote_images(self, user, record, max_frames) -> PreparedMedia:
        names = image_files(record)
        if not names:
            raise ContentFilterError("图文作品没有图片文件")
        chosen = pick_evenly(names, max_frames)
        images: List[bytes] = []
        async with httpx.AsyncClient(timeout=REMOTE_IMAGE_TIMEOUT_SECONDS, transport=self._transport,
                                     follow_redirects=True, trust_env=False) as client:
            for name in chosen:
                local = self._local.optional(name)
                if local is not None:
                    images.append(await asyncio.to_thread(shrink_image, local))
                    continue
                data = await _download_limited(client, await self._url(user, record, name))
                images.append(await asyncio.to_thread(shrink_image_bytes, data))
        return PreparedMedia(images, image_post_note(len(names), len(images)), source="archive")


async def _download_limited(client: httpx.AsyncClient, url: str) -> bytes:
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise ContentFilterError(f"网盘图片 HTTP {response.status_code}")
        chunks, total = [], 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_REMOTE_IMAGE_BYTES:
                raise ContentFilterError("网盘图片超过 20MB")
            chunks.append(chunk)
    return b"".join(chunks)
