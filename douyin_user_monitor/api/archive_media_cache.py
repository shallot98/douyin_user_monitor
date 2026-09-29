"""归档小文件的本机磁盘缓存。

D2 网关每个请求的首字节要 1~3 秒（实测，与连接复用无关），而 iOS Safari
播放一个视频至少串行发两次请求（先 bytes=0-1 探测，再取正文），每次都回源
就是 5~8 秒起步。归档文件内容不变，小文件整份拉一次落盘，后续任意 Range
都由本机直接应答。

只缓存不超过 max_file_bytes 的文件；超限或上游不给整份内容的记下来，之后直接走原来的流式代理。
总量超过 max_total_bytes 时按最近访问时间淘汰。磁盘剩余低于 min_free_bytes 时
不写新缓存并清空已有缓存，把空间让给其他程序，播放退回流式代理。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Tuple

logger = logging.getLogger(__name__)

StreamOpener = Callable[[], Awaitable[Tuple[int, Mapping[str, str], AsyncIterator[bytes]]]]

# 不可缓存记录只是省一次回源，数量到顶直接清空重来即可
UNCACHEABLE_MEMO_MAX_ITEMS = 10000
TEMP_SUFFIX = ".part"


class ArchiveMediaCache:
    def __init__(
        self, root: Path, *, max_file_bytes: int, max_total_bytes: int, min_free_bytes: int = 0
    ) -> None:
        self._root = root
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes
        self._min_free_bytes = min_free_bytes
        self._inflight: dict[str, asyncio.Task[Path | None]] = {}
        self._uncacheable: set[str] = set()

    @staticmethod
    def key_for(*parts: str) -> str:
        return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()

    def _path_for(self, key: str, suffix: str) -> Path:
        return self._root / f"{key}{suffix}"

    def lookup(self, key: str, suffix: str) -> Path | None:
        path = self._path_for(key, suffix)
        try:
            # 刷新 mtime 当作访问时间，淘汰时按它排序
            os.utime(path)
        except FileNotFoundError:
            return None
        return path

    async def get_or_fetch(self, key: str, suffix: str, opener: StreamOpener) -> Path | None:
        """返回缓存文件路径；文件超限、上游不给整份内容或磁盘余量不足时返回 None。

        同一文件的并发请求共用一次回源。回源失败的异常原样抛给调用方。
        """
        cached = self.lookup(key, suffix)
        if cached is not None:
            return cached
        if key in self._uncacheable:
            return None
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch(key, suffix, opener))
            self._inflight[key] = task
            task.add_done_callback(lambda _: self._inflight.pop(key, None))
        # shield：某个请求被取消（浏览器断开）不能连带取消共享的回源
        return await asyncio.shield(task)

    def _has_free_space(self) -> bool:
        # 缓存目录可能还没建，按其所在目录的文件系统算
        probe = self._root if self._root.exists() else self._root.parent
        free = shutil.disk_usage(probe).free
        if free >= self._min_free_bytes:
            return True
        if self._root.exists():
            logger.warning("磁盘剩余 %d MB 低于阈值，清空归档媒体缓存并暂停缓存", free // (1024 * 1024))
            for path in self._root.iterdir():
                if not path.name.endswith(TEMP_SUFFIX):
                    path.unlink(missing_ok=True)
        return False

    def _mark_uncacheable(self, key: str) -> None:
        if len(self._uncacheable) >= UNCACHEABLE_MEMO_MAX_ITEMS:
            self._uncacheable.clear()
        self._uncacheable.add(key)

    async def _fetch(self, key: str, suffix: str, opener: StreamOpener) -> Path | None:
        # 放在共享任务里查：get_or_fetch 建任务前不能有 await，否则并发请求会各自回源
        if not await asyncio.to_thread(self._has_free_space):
            return None
        status_code, headers, body = await opener()
        try:
            if status_code != 200:
                self._mark_uncacheable(key)
                return None
            declared = _content_length(headers)
            if declared is not None and declared > self._max_file_bytes:
                self._mark_uncacheable(key)
                return None
            final_path = self._path_for(key, suffix)
            temp_path = final_path.with_name(final_path.name + TEMP_SUFFIX)
            written = await self._write_body(temp_path, body, declared)
            if written is None:
                self._mark_uncacheable(key)
                return None
            await asyncio.to_thread(os.replace, temp_path, final_path)
        finally:
            await _release(body)
        await asyncio.to_thread(self._evict)
        return final_path

    async def _write_body(self, temp_path: Path, body: AsyncIterator[bytes], declared: int | None) -> int | None:
        """写临时文件；超限返回 None。长度与声明不符视为上游截断，抛错不落盘。"""
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        handle = await asyncio.to_thread(temp_path.open, "wb")
        written = 0
        completed = False
        try:
            async for chunk in body:
                written += len(chunk)
                if written > self._max_file_bytes:
                    return None
                await asyncio.to_thread(handle.write, chunk)
            if declared is not None and written != declared:
                raise OSError(f"归档文件读取不完整: {written}/{declared} bytes")
            completed = True
            return written
        finally:
            await asyncio.to_thread(handle.close)
            if not completed:
                await asyncio.to_thread(temp_path.unlink, missing_ok=True)

    def _evict(self) -> None:
        entries: list[tuple[float, int, Path]] = []
        for path in self._root.iterdir():
            if path.name.endswith(TEMP_SUFFIX):
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= self._max_total_bytes:
                break
            path.unlink(missing_ok=True)
            total -= size


async def _release(body: AsyncIterator[bytes]) -> None:
    """关闭上游 body。

    从未迭代过的 async generator 调 aclose 不会执行它的 finally，
    open_media_stream 里关 httpx client 的逻辑就跑不到，连接会泄漏；先推进一步再关。
    """
    try:
        await body.__anext__()
    except StopAsyncIteration:
        pass
    except Exception as exc:  # noqa: BLE001 - 已决定丢弃这个响应，清理失败只记录
        logger.warning("归档媒体上游响应关闭时出错: %s", exc)
    finally:
        await body.aclose()


def _content_length(headers: Mapping[str, Any]) -> int | None:
    for name, value in headers.items():
        if str(name).lower() == "content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None
