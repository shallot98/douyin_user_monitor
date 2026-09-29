"""Internal Cookie clearing must persist without weakening public input checks."""

import asyncio
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock

import yaml

from crawlers.douyin.web import config_loader
from crawlers.douyin.web.utils import _reload_token_manager_from_config
from douyin_user_monitor.crawler.inprocess_client import InProcessDouyinClient


class CookieRuntimeClearTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous_config = config_loader.get_config_path()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.restore_config)
        example = Path(__file__).parents[1] / "config/douyin_web.example.yaml"
        config = yaml.safe_load(example.read_text())
        config["TokenManager"]["douyin"]["headers"]["Cookie"] = "secret-for-test-only"
        self.path = Path(self.tmp.name) / "crawler.yaml"
        self.path.write_text(yaml.safe_dump(config))
        self.client = InProcessDouyinClient(self.path)

    def restore_config(self):
        config_loader.set_config_path(self.previous_config)
        _reload_token_manager_from_config()

    async def test_clear_persists_and_does_not_log_the_revoked_value(self):
        output = io.StringIO()
        with redirect_stdout(output):
            await self.client.clear_cookie()
        self.assertEqual(await self.client.get_cookie(), "")
        self.assertEqual(yaml.safe_load(self.path.read_text())["TokenManager"]["douyin"]["headers"]["Cookie"], "")
        restarted = InProcessDouyinClient(self.path)
        self.assertEqual(await restarted.get_cookie(), "")
        self.assertNotIn("secret-for-test-only", output.getvalue())

    async def test_normal_updates_still_reject_empty_values(self):
        with self.assertRaises(ValueError):
            await self.client.update_cookie(" ")
        self.assertEqual(await self.client.get_cookie(), "secret-for-test-only")

    async def test_clear_waits_for_temporary_cookie_before_persisting(self):
        async with self.client.temporary_cookie("probe-cookie"):
            clearing = asyncio.create_task(self.client.clear_cookie())
            await asyncio.sleep(0)
            self.assertFalse(clearing.done())
            self.assertEqual(self.client._headers_bucket()["Cookie"], "probe-cookie")
        await clearing
        self.assertEqual(await self.client.get_cookie(), "")

    async def test_failed_persistence_restores_runtime_value_and_raises(self):
        async def fail(value):
            self.client._headers_bucket()["Cookie"] = value
            raise OSError("write failed")

        self.client._crawler.update_cookie = AsyncMock(side_effect=fail)
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            await self.client.clear_cookie()
        self.assertEqual(await self.client.get_cookie(), "secret-for-test-only")
        self.assertEqual(yaml.safe_load(self.path.read_text())["TokenManager"]["douyin"]["headers"]["Cookie"],
                         "secret-for-test-only")
