"""Inspect local media before publishing a browser playback file."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

FAST_PLAY_MAX_SHORT_SIDE = 720
FAST_PLAY_MAX_LONG_SIDE = 1280
FAST_PLAY_MAX_BIT_RATE = 1_600_000
MEDIA_PROCESS_NICENESS = 10
MP4_ATOM_HEADER_SIZE = 8
MP4_EXTENDED_SIZE_MARKER = 1


@dataclass(frozen=True, kw_only=True)
class PlaybackMedia:
    width: int
    height: int
    bit_rate: int
    duration: float
    video_codec: str
    pixel_format: str
    audio_codecs: tuple[str, ...]
    is_mp4: bool
    faststart: bool

    @property
    def lightweight(self) -> bool:
        return (
            self.is_mp4
            and self.video_codec == "h264"
            and self.pixel_format in {"yuv420p", "yuvj420p"}
            and all(codec == "aac" for codec in self.audio_codecs)
            and min(self.width, self.height) <= FAST_PLAY_MAX_SHORT_SIDE
            and max(self.width, self.height) <= FAST_PLAY_MAX_LONG_SIDE
            and self.bit_rate <= FAST_PLAY_MAX_BIT_RATE
        )

    @property
    def ready(self) -> bool:
        return self.lightweight and self.faststart


async def run_media_tool(arguments: Sequence[str]) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "nice", "-n", str(MEDIA_PROCESS_NICENESS), *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(
            f"{arguments[0]} failed ({process.returncode}): {error.decode(errors='replace').strip()}"
        )
    return output


async def probe_playback_media(path: Path) -> PlaybackMedia:
    output = await run_media_tool((
        "ffprobe", "-v", "error", "-threads", "1", "-show_entries",
        "stream=codec_type,codec_name,pix_fmt,width,height:format=format_name,bit_rate,duration",
        "-of", "json", str(path),
    ))
    data = json.loads(output)
    streams = data.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    if not videos:
        raise ValueError(f"Playback source has no video stream: {path.name}")
    video, container = videos[0], data["format"]
    width, height = int(video["width"]), int(video["height"])
    duration = float(container["duration"])
    if min(width, height, duration) <= 0:
        raise ValueError(f"Invalid playback dimensions or duration: {path.name}")
    is_mp4 = "mp4" in container.get("format_name", "").split(",")
    return PlaybackMedia(
        width=width, height=height, duration=duration,
        bit_rate=int(container.get("bit_rate") or path.stat().st_size * 8 / duration),
        video_codec=video.get("codec_name", ""), pixel_format=video.get("pix_fmt", ""),
        audio_codecs=tuple(s.get("codec_name", "") for s in streams if s.get("codec_type") == "audio"),
        is_mp4=is_mp4, faststart=is_mp4 and has_faststart(path),
    )


def has_faststart(path: Path) -> bool:
    """Read top-level atom headers only; never scan the video payload for 'moov'."""
    file_size = path.stat().st_size
    seen_moov = False
    with path.open("rb") as stream:
        while stream.tell() < file_size:
            offset = stream.tell()
            header = stream.read(MP4_ATOM_HEADER_SIZE)
            if len(header) != MP4_ATOM_HEADER_SIZE:
                raise ValueError(f"Truncated MP4 atom: {path.name}")
            size = int.from_bytes(header[:4], "big")
            header_size = MP4_ATOM_HEADER_SIZE
            if size == MP4_EXTENDED_SIZE_MARKER:
                size = int.from_bytes(stream.read(MP4_ATOM_HEADER_SIZE), "big")
                header_size += MP4_ATOM_HEADER_SIZE
            elif size == 0:
                size = file_size - offset
            if size < header_size or offset + size > file_size:
                raise ValueError(f"Invalid MP4 atom size: {path.name}")
            if header[4:] == b"moov":
                seen_moov = True
            if header[4:] == b"mdat":
                return seen_moov
            stream.seek(offset + size)
    return False
