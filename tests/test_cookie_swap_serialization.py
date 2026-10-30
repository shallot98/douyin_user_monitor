from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List

from douyin_user_monitor.crawler.inprocess_client import InProcessDouyinClient


def _make_client(shared_headers: Dict[str, Any]) -> InProcessDouyinClient:
    """构造不触发 vendored crawler import 的最小实例：绕过 __init__，手工注入锁与 headers 桶。"""
    client = object.__new__(InProcessDouyinClient)
    client._cookie_lock = asyncio.Lock()
    client._headers_bucket = lambda: shared_headers  # type: ignore[method-assign]
    return client


class TemporaryCookieSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_swaps_do_not_interleave(self):
        shared = {"Cookie": "orig"}
        client = _make_client(shared)
        violations: List[Any] = []

        async def worker(value: str) -> None:
            async with client.temporary_cookie(value):
                # 串行化下，进入区间内看到的 Cookie 必须恒等于自己换入的值。
                for _ in range(8):
                    await asyncio.sleep(0)
                    observed = client._headers_bucket().get("Cookie")
                    if observed != value:
                        violations.append((value, observed))

        await asyncio.gather(worker("cookie-A"), worker("cookie-B"))
        self.assertEqual(violations, [])

    async def test_restores_original_after_completion(self):
        shared = {"Cookie": "orig"}
        client = _make_client(shared)
        async with client.temporary_cookie("temp"):
            self.assertEqual(shared["Cookie"], "temp")
        self.assertEqual(shared["Cookie"], "orig")
        self.assertFalse(client._cookie_lock.locked())

    async def test_exception_releases_lock_and_restores(self):
        shared = {"Cookie": "orig"}
        client = _make_client(shared)

        with self.assertRaises(RuntimeError):
            async with client.temporary_cookie("temp"):
                self.assertEqual(shared["Cookie"], "temp")
                raise RuntimeError("boom")

        self.assertEqual(shared["Cookie"], "orig")
        self.assertFalse(client._cookie_lock.locked())
        # 锁确已释放：后续换入可正常获取。
        async with client.temporary_cookie("temp2"):
            self.assertEqual(shared["Cookie"], "temp2")
        self.assertEqual(shared["Cookie"], "orig")


if __name__ == "__main__":
    unittest.main()
