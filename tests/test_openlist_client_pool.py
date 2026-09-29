"""OpenList 查询超时不能把连接池耗尽（2026-09-28 线上事故回归）。

旧实现用 asyncio.wait_for 从外部取消 httpx 请求；httpcore 在建连/排队时被取消会把连接
永久留在池里，池满后所有播放都卡在「OpenList 查询超时」。
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from douyin_user_monitor.monitor import openlist_client as openlist_module
from douyin_user_monitor.monitor.openlist_client import ArchiveUnavailableError, OpenListClient
from douyin_user_monitor.settings import ArchiveSettings

STALL_ROUNDS = 40
CONCURRENT_RESOLVES = 20
POOL_LIMIT = 4


def build_client(base_url: str) -> OpenListClient:
    return OpenListClient(
        ArchiveSettings(
            enabled=True,
            openlist_base_url=base_url,
            openlist_db_path=Path("unused.db"),
            openlist_token="test-token",
            primary_base="/d2/vps/抖音视频",
            backup_base="/kuake/抖音视频",
            timeout_seconds=30.0,
            sanitize_primary_dirs=False,
        )
    )


def busy_connections(client: httpx.AsyncClient) -> list:
    pool = client._transport._pool  # noqa: SLF001
    return [conn for conn in pool.connections if not conn.is_idle() and not conn.is_closed()]


class OpenListPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_openlist_does_not_leak_pool_connections(self):
        """OpenList 时快时慢、查询反复超时后，连接池仍能服务新请求。"""
        delays = iter([])

        async def flaky_openlist(reader, writer):
            try:
                while await reader.read(65536):
                    await asyncio.sleep(next(delays, 0.0))
                    body = b'{"code":200,"data":{"raw_url":"https://d2.example.test/v.mp4"}}'
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
                    await writer.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass

        server = await asyncio.start_server(flaky_openlist, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = build_client(f"http://127.0.0.1:{port}")
        client._http_client = httpx.AsyncClient(  # noqa: SLF001
            timeout=30.0, limits=httpx.Limits(max_connections=POOL_LIMIT, max_keepalive_connections=2)
        )
        # 一半请求慢于候选超时，另一半正常，制造排队 + 超时交错
        delays = iter([0.3 if i % 2 else 0.0 for i in range(STALL_ROUNDS * CONCURRENT_RESOLVES * 2)])
        try:
            with patch.object(openlist_module, "RESOLVE_CANDIDATE_TIMEOUT_SECONDS", 0.1):
                for round_index in range(STALL_ROUNDS):
                    client._url_cache.clear()  # noqa: SLF001
                    await asyncio.gather(
                        *(client.resolve_stream_url(f"u/{round_index}-{i}.mp4") for i in range(CONCURRENT_RESOLVES)),
                        return_exceptions=True,
                    )
            await asyncio.sleep(0.5)
            http_client = client._http_client  # noqa: SLF001
            self.assertIsNotNone(http_client)
            self.assertLessEqual(len(busy_connections(http_client)), POOL_LIMIT - 1)
            client._url_cache.clear()  # noqa: SLF001
            delays = iter([])
            url = await client.resolve_stream_url("u/fresh.mp4")
            self.assertEqual(url, "https://d2.example.test/v.mp4")
        finally:
            await client.close()
            server.close()

    async def test_pool_timeout_recycles_http_client(self):
        def exhausted(request: httpx.Request) -> httpx.Response:
            raise httpx.PoolTimeout("pool exhausted", request=request)

        client = build_client("http://openlist.test")
        stale = httpx.AsyncClient(transport=httpx.MockTransport(exhausted))
        client._http_client = stale  # noqa: SLF001
        with self.assertRaisesRegex(ArchiveUnavailableError, "连接池已满"):
            await client.resolve_stream_url("u/a.mp4")
        self.assertTrue(stale.is_closed)
        self.assertIsNot(await client._get_http_client(), stale)  # noqa: SLF001
        await client.close()

    async def test_candidate_timeout_is_passed_to_httpx(self):
        seen: list[float | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions["timeout"]["read"])
            return httpx.Response(200, json={"code": 200, "data": {"raw_url": "https://d2.example.test/v.mp4"}})

        client = build_client("http://openlist.test")
        client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0)  # noqa: SLF001
        await client.resolve_stream_url("u/a.mp4")
        self.assertEqual(seen, [openlist_module.RESOLVE_CANDIDATE_TIMEOUT_SECONDS])
        await client.close()

    async def test_httpx_timeout_falls_back_to_backup(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if b"/d2/" in request.content:
                raise httpx.ReadTimeout("stalled", request=request)
            return httpx.Response(200, json={"code": 200, "data": {"raw_url": "https://quark.example.test/v.mp4"}})

        client = build_client("http://openlist.test")
        client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
        self.assertEqual(await client.resolve_stream_url("u/a.mp4"), "https://quark.example.test/v.mp4")
        await client.close()


if __name__ == "__main__":
    unittest.main()
