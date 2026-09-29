"""Prepare a separate H.264/AAC faststart file without changing archive bytes.

Use a supplied lightweight H.264 variant when available. Otherwise preparation
from the saved archive is an explicit step: stream-copy compatible media, or
encode a 540p playback copy. Failures propagate and never promote partial files.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from douyin_user_monitor.monitor.playback_media import (
    PlaybackMedia,
    probe_playback_media,
    run_media_tool,
)
from douyin_user_monitor.monitor.video_source import VideoSource

logger = logging.getLogger(__name__)

ENCODE_SHORT_SIDE = 540
ENCODE_LONG_SIDE = 960
ENCODE_FRAME_RATE = 30
ENCODE_KEYFRAME_INTERVAL = 60
ENCODE_CRF = 26
ENCODE_MAX_BIT_RATE = 1_200_000
ENCODE_BUFFER_SIZE = 2_400_000
ENCODE_AUDIO_BIT_RATE = 96_000
COVER_MAX_WIDTH = 360
COVER_JPEG_QUALITY = 4


class ArchiveMediaInspectionError(RuntimeError):
    """The saved archive was rejected by the existing media inspection step."""


@dataclass(frozen=True, kw_only=True)
class PlaybackRequest:
    archive_path: Path
    target_path: Path
    candidate_path: Path | None = None


@dataclass(frozen=True, kw_only=True)
class PreparedPlayback:
    path: Path
    media: PlaybackMedia
    method: str
    created: bool

    def to_video_source(self) -> VideoSource:
        return VideoSource(
            url="", width=self.media.width, height=self.media.height,
            bit_rate=self.media.bit_rate, gear_name=f"h264_{self.method}",
            quality_source=f"playback_{self.method}",
        )


PlaybackPreparer = Callable[[PlaybackRequest], Awaitable[PreparedPlayback]]
CoverPreparer = Callable[[Path, Path], Awaitable[None]]


async def prepare_playback(request: PlaybackRequest) -> PreparedPlayback:
    if request.archive_path.resolve() == request.target_path.resolve():
        raise ValueError("Playback output must not replace the original archive")
    if request.target_path.exists():
        media = await probe_playback_media(request.target_path)
        if media.ready:
            return PreparedPlayback(path=request.target_path, media=media, method="verified", created=False)
        logger.info("Preparing existing incompatible playback file=%s", request.target_path.name)
    source, media = await _select_preparation_input(request)
    if source == request.archive_path and media.ready:
        return PreparedPlayback(path=source, media=media, method="reuse", created=False)
    if source == request.candidate_path and media.ready:
        os.replace(source, request.target_path)
        return PreparedPlayback(path=request.target_path, media=media, method="download", created=True)
    method = "remux" if media.lightweight else "transcode"
    logger.info("Preparing playback method=%s source=%s output=%s", method, source.name, request.target_path.name)
    output_media = await _write_prepared_file(source, request.target_path, method=method)
    return PreparedPlayback(path=request.target_path, media=output_media, method=method, created=True)


async def _select_preparation_input(request: PlaybackRequest) -> tuple[Path, PlaybackMedia]:
    try:
        archive = await probe_playback_media(request.archive_path)
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        raise ArchiveMediaInspectionError(str(error)) from error
    if request.candidate_path is None:
        logger.info("Checking saved archive for playback preparation file=%s", request.archive_path.name)
        return request.archive_path, archive
    candidate = await probe_playback_media(request.candidate_path)
    if not candidate.lightweight:
        logger.warning(
            "Supplied playback requires conversion; preparing archive instead file=%s codec=%s",
            request.archive_path.name, candidate.video_codec,
        )
        return request.archive_path, archive
    if archive.ready and request.archive_path.stat().st_size <= request.candidate_path.stat().st_size:
        return request.archive_path, archive
    return request.candidate_path, candidate


async def _write_prepared_file(source: Path, target: Path, *, method: str) -> PlaybackMedia:
    partial = target.with_name(f"{target.name}.processing.part")
    codec_options = ("-c", "copy") if method == "remux" else _encode_options()
    try:
        await run_media_tool((
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-xerror",
            "-threads", "1", "-filter_threads", "1", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1",
            *codec_options, "-threads", "1", "-movflags", "+faststart", "-f", "mp4", str(partial),
        ))
        media = await probe_playback_media(partial)
        if not media.ready:
            raise ValueError(f"Prepared playback is not lightweight H.264/AAC faststart: {target.name}")
        os.replace(partial, target)
        return media
    finally:
        partial.unlink(missing_ok=True)


def _encode_options() -> tuple[str, ...]:
    width = f"min(iw,if(gte(iw,ih),{ENCODE_LONG_SIDE},{ENCODE_SHORT_SIDE}))"
    height = f"min(ih,if(gte(iw,ih),{ENCODE_SHORT_SIDE},{ENCODE_LONG_SIDE}))"
    scale = f"scale=w='{width}':h='{height}':force_original_aspect_ratio=decrease:force_divisible_by=2"
    return (
        "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-crf", str(ENCODE_CRF),
        "-pix_fmt", "yuv420p", "-r", str(ENCODE_FRAME_RATE), "-g", str(ENCODE_KEYFRAME_INTERVAL),
        "-maxrate", str(ENCODE_MAX_BIT_RATE), "-bufsize", str(ENCODE_BUFFER_SIZE),
        "-c:a", "aac", "-b:a", str(ENCODE_AUDIO_BIT_RATE),
    )


async def prepare_cover(source: Path, target: Path) -> None:
    """Use the first frame/image as a thumbnail only when no supplied cover exists."""
    if source.resolve() == target.resolve():
        raise ValueError("Cover output must not replace its source")
    partial = target.with_name(f"{target.name}.processing.part")
    logger.info("Preparing cover from saved media source=%s output=%s", source.name, target.name)
    try:
        await run_media_tool((
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-xerror",
            "-threads", "1", "-filter_threads", "1", "-i", str(source),
            "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale='min(iw,{COVER_MAX_WIDTH})':-2",
            "-c:v", "mjpeg", "-q:v", str(COVER_JPEG_QUALITY), "-threads", "1",
            "-f", "image2", "-update", "1", str(partial),
        ))
        if not partial.stat().st_size:
            raise ValueError(f"Prepared cover is empty: {target.name}")
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
