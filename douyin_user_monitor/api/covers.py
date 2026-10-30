"""Small local cover responses; the monitor router supplies authentication."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import Response

from douyin_user_monitor.monitor.archive_feed import find_record
from douyin_user_monitor.monitor.cover_store import CoverIntegrityError, CoverMetadata, CoverStore

COVER_CACHE_SECONDS = 86400
logger = logging.getLogger(__name__)


def _cover_url(cover: CoverMetadata) -> str:
    user_id = quote(cover.user_id, safe="")
    aweme_id = quote(cover.aweme_id, safe="")
    return f"/api/monitor/users/{user_id}/archive/{aweme_id}/cover?v={cover.version}"


async def with_cover_urls(
    store: CoverStore, user_id: str, items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    covers = await asyncio.to_thread(store.lookup_many, user_id, items)
    return [
        {**item, "cover_url": _cover_url(covers[item["aweme_id"]]) if item["aweme_id"] in covers else None}
        for item in items
    ]


def _matches_etag(request: Request, etag: str) -> bool:
    values = request.headers.get("if-none-match", "").split(",")
    return any(value.strip().removeprefix("W/") in (etag, "*") for value in values)


async def cover_response(
    request: Request, user: dict[str, Any], store: CoverStore, *, aweme_id: str, version: str | None,
) -> Response:
    record = find_record(user, aweme_id)
    if record is None:
        raise HTTPException(status_code=404, detail="作品不在下载记录中")
    try:
        cover = await asyncio.to_thread(store.lookup, user["id"], record)
        if cover is None or (version is not None and cover.version != version):
            raise HTTPException(status_code=404, detail="封面或封面版本不存在")
        data = await asyncio.to_thread(store.read, cover)
    except (CoverIntegrityError, OSError) as exc:
        logger.error("Cover verification failed: user=%s aweme=%s", user["id"], aweme_id, exc_info=True)
        raise HTTPException(status_code=500, detail="封面文件校验失败") from exc
    etag = f'"{cover.version}"'
    headers = {
        "Cache-Control": f"private, max-age={COVER_CACHE_SECONDS}, immutable" if version else "private, no-cache",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    if _matches_etag(request, etag):
        return Response(status_code=304, headers=headers)
    return Response(data, media_type="image/jpeg", headers=headers)
