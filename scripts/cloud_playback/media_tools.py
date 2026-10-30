"""Single-threaded media commands for the independent repair host."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path

from douyin_user_monitor.monitor.playback_media import MEDIA_PROCESS_NICENESS, PlaybackMedia, has_faststart

SHORT_SIDE = 540
LONG_SIDE = 960
FRAME_RATE = 30
KEYFRAME_INTERVAL = 60
CRF = 26
MAX_BIT_RATE = 1_200_000
BUFFER_SIZE = 2_400_000
AUDIO_BIT_RATE = 96_000
COVER_WIDTH = 360
COVER_QUALITY = 4
DURATION_MIN_TOLERANCE = 0.5
DURATION_RELATIVE_TOLERANCE = 0.01
MICROSECONDS_PER_SECOND = 1_000_000
PROXY_VARIABLES = {"http_proxy", "https_proxy", "all_proxy"}
MEDIA_FIELDS = tuple(PlaybackMedia.__dataclass_fields__)
PROBE_FIELDS = (
    "stream=codec_type,codec_name,pix_fmt,width,height,avg_frame_rate,nb_frames:"
    "format=format_name,bit_rate,duration"
)


class MediaJobError(RuntimeError):
    """A media failure with no signed URLs in its public diagnostic."""


def run_media(arguments, *, timeout=None):
    environment = {key: value for key, value in os.environ.items() if key.lower() not in PROXY_VARIABLES}
    environment.update(no_proxy="*", NO_PROXY="*")
    try:
        process = subprocess.run(
            ["nice", "-n", str(MEDIA_PROCESS_NICENESS), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise MediaJobError(f"{arguments[0]} timed out after {timeout:g} seconds") from None
    except OSError as error:
        raise MediaJobError(f"{arguments[0]} could not start ({type(error).__name__})") from None
    if process.returncode:
        diagnostic = re.sub(r"https?://[^\s]+", "[private URL]", process.stderr.decode(errors="replace"))
        raise MediaJobError(f"{arguments[0]} failed ({process.returncode}): {diagnostic.strip()}")
    return process.stdout


def _probe(location, *, timeout=None):
    raw = run_media((
        "ffprobe", "-v", "error", "-threads", "1", "-show_entries", PROBE_FIELDS,
        "-of", "json", str(location),
    ), timeout=timeout)
    try:
        data = json.loads(raw)
        streams = data["streams"]
        videos = [stream for stream in streams if stream.get("codec_type") == "video"]
        if not videos:
            raise MediaJobError("Media has no video stream")
        return videos[0], streams, data["format"]
    except (ValueError, KeyError, TypeError):
        raise MediaJobError("ffprobe returned invalid media metadata") from None


def probe_video(location, *, local=False):
    video, streams, container = _probe(location)
    try:
        duration = float(container["duration"])
        width, height = int(video["width"]), int(video["height"])
        bit_rate = int(container["bit_rate"])
        frame_rate = float(Fraction(video.get("avg_frame_rate", "0/1")))
        frames = video.get("nb_frames")
        frames = int(frames) if frames not in (None, "N/A") else None
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        raise MediaJobError("ffprobe returned invalid video dimensions, duration, bitrate or frame rate") from None
    if not math.isfinite(duration) or min(width, height, duration, bit_rate) <= 0:
        raise MediaJobError("Video dimensions, duration and bitrate must be positive")
    is_mp4 = "mp4" in container.get("format_name", "").split(",")
    media = PlaybackMedia(
        width=width, height=height, bit_rate=bit_rate, duration=duration,
        video_codec=video.get("codec_name", ""), pixel_format=video.get("pix_fmt", ""),
        audio_codecs=tuple(stream.get("codec_name", "") for stream in streams if stream.get("codec_type") == "audio"),
        is_mp4=is_mp4, faststart=bool(local and is_mp4 and has_faststart(Path(location))),
    )
    return {**asdict(media), "frame_rate": frame_rate, "declared_frames": frames}


def probe_cover(location, *, timeout=None):
    video, streams, _ = _probe(location, timeout=timeout)
    try:
        width, height = int(video["width"]), int(video["height"])
    except (KeyError, ValueError, TypeError):
        raise MediaJobError("ffprobe returned invalid cover dimensions") from None
    if video.get("codec_name") != "mjpeg" or min(width, height) <= 0 or width > COVER_WIDTH:
        raise MediaJobError("Cover must be a nonempty JPEG at the requested size")
    if any(stream.get("codec_type") == "audio" for stream in streams):
        raise MediaJobError("Cover unexpectedly contains audio")
    return {"video_codec": "mjpeg", "width": width, "height": height}


def assert_duration(actual, expected):
    tolerance = max(DURATION_MIN_TOLERANCE, expected * DURATION_RELATIVE_TOLERANCE)
    if not math.isfinite(actual) or abs(actual - expected) > tolerance:
        raise MediaJobError(f"Duration mismatch: actual={actual:.3f}s expected={expected:.3f}s")


def validate_playback(media, source, *, expected_duration, operation):
    inspected = PlaybackMedia(**{key: media[key] for key in MEDIA_FIELDS})
    if not inspected.ready or inspected.pixel_format != "yuv420p":
        raise MediaJobError("Playback must be lightweight H.264/AAC/yuv420p MP4 with faststart")
    if bool(media["audio_codecs"]) != bool(source["audio_codecs"]):
        raise MediaJobError("Playback audio presence differs from the source")
    if operation == "transcode":
        if min(media["width"], media["height"]) > SHORT_SIDE or max(media["width"], media["height"]) > LONG_SIDE:
            raise MediaJobError("Transcoded dimensions exceed the requested playback size")
        if not math.isclose(media["frame_rate"], FRAME_RATE):
            raise MediaJobError("Transcoded frame rate differs from the requested playback rate")
    if not media["declared_frames"] or media["declared_frames"] <= 0:
        raise MediaJobError("Playback has no declared video frames")
    assert_duration(media["duration"], expected_duration)


def assert_remux_source(media):
    inspected = PlaybackMedia(**{key: media[key] for key in MEDIA_FIELDS})
    if not inspected.lightweight or inspected.pixel_format != "yuv420p":
        raise MediaJobError("Remux source is not already lightweight H.264/AAC/yuv420p")


def _ffmpeg_input(location):
    return (
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-xerror",
        "-err_detect", "explode", "-threads", "1", "-filter_threads", "1", "-i", str(location),
    )


def _encode_options():
    width = f"min(iw,if(gte(iw,ih),{LONG_SIDE},{SHORT_SIDE}))"
    height = f"min(ih,if(gte(iw,ih),{SHORT_SIDE},{LONG_SIDE}))"
    scale = f"scale=w='{width}':h='{height}':force_original_aspect_ratio=decrease:force_divisible_by=2"
    return (
        "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF),
        "-pix_fmt", "yuv420p", "-r", str(FRAME_RATE), "-g", str(KEYFRAME_INTERVAL),
        "-maxrate", str(MAX_BIT_RATE), "-bufsize", str(BUFFER_SIZE),
        "-c:a", "aac", "-b:a", str(AUDIO_BIT_RATE),
    )


def prepare_video(location, target, operation):
    codecs = ("-c", "copy") if operation == "remux" else _encode_options()
    run_media((
        *_ffmpeg_input(location), "-map", "0:v:0", "-map", "0:a:0?",
        "-map_metadata", "-1", "-map_chapters", "-1", *codecs,
        "-threads", "1", "-movflags", "+faststart", "-f", "mp4", str(target),
    ))


def prepare_cover(location, target, *, timeout=None):
    run_media((
        *_ffmpeg_input(location), "-map", "0:v:0", "-frames:v", "1",
        "-vf", f"scale='min(iw,{COVER_WIDTH})':-2", "-c:v", "mjpeg", "-q:v", str(COVER_QUALITY),
        "-threads", "1", "-f", "image2", "-update", "1", str(target),
    ), timeout=timeout)


def full_decode(location, *, expected_frames, expected_duration=None, timeout=None):
    raw = run_media((
        *_ffmpeg_input(location), "-map", "0:v:0", "-map", "0:a:0?", "-vsync", "0",
        "-progress", "pipe:1", "-nostats", "-threads", "1", "-f", "null", "-",
    ), timeout=timeout)
    progress = dict(line.split("=", 1) for line in raw.decode(errors="replace").splitlines() if "=" in line)
    try:
        frames = int(progress["frame"])
        duration = int(progress["out_time_us"]) / MICROSECONDS_PER_SECOND
    except (KeyError, ValueError):
        raise MediaJobError("Decoder did not report completed frames and duration") from None
    if progress.get("progress") != "end" or frames <= 0 or frames != expected_frames:
        raise MediaJobError("Decoder did not complete every declared video frame")
    if expected_duration is not None:
        assert_duration(duration, expected_duration)
    return {"full_decode_verified": True, "decoded_frames": frames, "decoded_duration": duration}
