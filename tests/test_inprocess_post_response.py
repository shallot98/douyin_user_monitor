from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from douyin_user_monitor.crawler.inprocess_client import InProcessDouyinClient
from douyin_user_monitor.monitor.cookie_rotation import is_auth_like_error


class PostResponseTests(unittest.IsolatedAsyncioTestCase):
    async def _fetch(self, payload):
        client = object.__new__(InProcessDouyinClient)
        client._crawler = SimpleNamespace(fetch_user_post_videos=AsyncMock(return_value=payload))
        return await client.fetch_user_post_videos("target-user", 0, 18)

    async def test_logged_out_preview_is_an_auth_failure_even_when_tip_is_hidden(self):
        for tip_visible in (True, False):
            with self.subTest(tip_visible=tip_visible):
                data = {"status_code": 0, "aweme_list": [{"aweme_id": "preview-work"}],
                        "not_login_module": {"guide_login_tip_exist": tip_visible}}
                with self.assertRaisesRegex(RuntimeError, "login required") as caught:
                    await self._fetch(data)
                self.assertTrue(is_auth_like_error(str(caught.exception)))

    async def test_status_only_response_is_not_an_empty_successful_page(self):
        with self.assertRaisesRegex(ValueError, "aweme_list"):
            await self._fetch({"status_code": 0})

    async def test_explicit_null_list_is_valid_for_an_account_with_no_public_posts(self):
        data = {"status_code": 0, "aweme_list": None, "time_list": None, "log_pb": {}}
        self.assertEqual(await self._fetch(data), data)

    async def test_non_list_post_payload_is_invalid(self):
        with self.assertRaisesRegex(ValueError, "aweme_list"):
            await self._fetch({"status_code": 0, "aweme_list": {"unexpected": "object"}})

    async def test_explicit_empty_page_remains_valid(self):
        data = {"status_code": 0, "aweme_list": [], "has_more": 0}
        self.assertEqual(await self._fetch(data), data)

    async def test_authenticated_post_list_passes_through(self):
        data = {"status_code": 0, "aweme_list": [{"aweme_id": "work"}], "has_more": 1}
        self.assertEqual(await self._fetch(data), data)


if __name__ == "__main__":
    unittest.main()
