"""Streaming media proxy for Douyin CDN.

Why proxy instead of letting the browser hit the CDN directly: the video CDN
rejects requests carrying our panel's own Referer (verified 403), while the
same URL with crawler headers streams fine. Proxying also keeps signed CDN
URLs out of the page source.

Range is passed through both ways so the browser can seek.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, Dict, Mapping, Tuple

import httpx

# (响应头规范名, 查找用小写名) — 只透传播放必需的头，
# 上游 Set-Cookie / CORS / Content-Disposition 一律不转给浏览器
PASSTHROUGH_RESPONSE_HEADERS: Tuple[Tuple[str, str], ...] = (
    ("Content-Type", "content-type"),
    ("Content-Length", "content-length"),
    ("Content-Range", "content-range"),
    ("Accept-Ranges", "accept-ranges"),
    ("ETag", "etag"),
    ("Last-Modified", "last-modified"),
)

STREAMABLE_STATUS_CODES = (200, 206)
DEFAULT_CHUNK_SIZE = 64 * 1024


class MediaUpstreamError(RuntimeError):
    """Upstream refused or failed before any bytes could be streamed."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def build_upstream_headers(base_headers: Mapping[str, str] | None, range_header: str | None) -> Dict[str, str]:
    """Crawler headers + the client's Range. Never inject our own Referer."""
    headers = {str(key): str(value) for key, value in (base_headers or {}).items()}
    # Media responses must keep their byte layout.  Asking the archive/CDN for
    # gzip or brotli makes Content-Length/Content-Range describe a different
    # representation than the bytes consumed by the browser.
    headers["Accept-Encoding"] = "identity"
    range_text = str(range_header or "").strip()
    if range_text:
        headers["Range"] = range_text
    return headers


def build_response_headers(
    upstream_headers: Mapping[str, str] | None,
    *,
    cache_seconds: int = 0,
) -> Dict[str, str]:
    lowered = {str(key).lower(): str(value) for key, value in (upstream_headers or {}).items()}
    encoding = lowered.get("content-encoding", "").strip().lower()
    # httpx 已替我们解压，上游长度与实际写出字节不可比（同 downloader 的 gzip 处理）
    compressed = bool(encoding) and encoding != "identity"

    headers: Dict[str, str] = {}
    for canonical_name, lookup_name in PASSTHROUGH_RESPONSE_HEADERS:
        if compressed and lookup_name == "content-length":
            continue
        value = lowered.get(lookup_name)
        if value:
            headers[canonical_name] = value

    headers.setdefault("Accept-Ranges", "bytes")
    if cache_seconds > 0:
        headers["Cache-Control"] = f"private, max-age={cache_seconds}"
    else:
        headers["Cache-Control"] = "no-store"
    return headers


def parse_byte_range(range_header: str | None, file_size: int) -> Tuple[int, int] | None:
    """Parse a single-range `bytes=` header into inclusive (start, end).

    Returns None when there is no usable range (caller should serve the whole
    file). Raises UnsatisfiableRangeError when the range falls outside the file.
    Multi-range requests are not supported and fall back to the full body.
    """
    text = str(range_header or "").strip().lower()
    if not text.startswith("bytes=") or file_size <= 0:
        return None
    spec = text[len("bytes=") :].strip()
    if "," in spec:
        return None

    start_text, _, end_text = spec.partition("-")
    start_text, end_text = start_text.strip(), end_text.strip()
    try:
        if not start_text:
            if not end_text:
                return None
            # bytes=-N —— 末尾 N 字节
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise UnsatisfiableRangeError(file_size)
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
    except UnsatisfiableRangeError:
        # 它继承 ValueError，必须排在下面的兜底之前，否则自己抛的会被吞掉
        raise
    except ValueError:
        return None

    if start < 0 or start >= file_size or end < start:
        raise UnsatisfiableRangeError(file_size)
    return start, min(end, file_size - 1)


class UnsatisfiableRangeError(ValueError):
    """Requested range lies outside the file (HTTP 416)."""

    def __init__(self, file_size: int) -> None:
        super().__init__(f"range not satisfiable for size {file_size}")
        self.file_size = file_size


def iter_file_range(path: Path, start: int, end: int, chunk_size: int = DEFAULT_CHUNK_SIZE):
    """Yield bytes [start, end] inclusive from a local file."""

    def _generator():
        remaining = end - start + 1
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return _generator()


async def open_media_stream(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> Tuple[int, Dict[str, str], AsyncIterator[bytes]]:
    """Open an upstream stream, returning (status, headers, body iterator).

    The returned iterator owns the httpx client/response and releases both when
    it finishes or the client disconnects, so it must always be consumed by a
    StreamingResponse.
    """
    client = httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds)
    request_headers = {str(key): str(value) for key, value in headers.items()}
    request_headers["Accept-Encoding"] = "identity"
    stream_context = client.stream("GET", url, headers=request_headers)
    try:
        response = await stream_context.__aenter__()
    except httpx.HTTPError as exc:
        await client.aclose()
        raise MediaUpstreamError(f"媒体拉取失败: {exc}") from exc

    # 416 describes the caller's Range, not an unavailable replica. Preserve its
    # Content-Range and body instead of retrying a different representation.
    range_rejected = response.status_code == 416 and any(
        key.lower() == "range" and value.strip() for key, value in request_headers.items()
    )
    if response.status_code not in STREAMABLE_STATUS_CODES and not range_rejected:
        status_code = response.status_code
        await stream_context.__aexit__(None, None, None)
        await client.aclose()
        raise MediaUpstreamError(f"媒体拉取失败: status={status_code}", status_code=status_code)

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await stream_context.__aexit__(None, None, None)
            await client.aclose()

    return response.status_code, dict(response.headers), body()
