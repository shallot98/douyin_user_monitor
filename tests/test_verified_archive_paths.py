from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from douyin_user_monitor.monitor.openlist_client import ArchiveUnavailableError, OpenListClient
from douyin_user_monitor.settings import ArchiveSettings

D2_BASE = "/d2/vps/抖音视频"
QUARK_BASE = "/kuake/抖音视频"
ORIGINAL_PATH = "小妮💃/作品🍬.mp4"
SANITIZED_PATH = "小妮/作品.mp4"
DIRECTORY_ONLY_PATH = "小妮/作品🍬.mp4"
VERIFIED_PATH = f"{D2_BASE}/sec_sec-1/原来的文件名  空格.mp4"
VERIFIED_QUARK_PATH = f"{QUARK_BASE}/sec_sec-1/原来的文件名  空格.mp4"
D2_URL = "https://d2.example.test/video.mp4"


class VerifiedArchivePathsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = OpenListClient(
            ArchiveSettings(
                enabled=True,
                openlist_base_url="http://127.0.0.1:5244",
                openlist_db_path=Path("unused.db"),
                openlist_token="test-token",
                primary_base=D2_BASE,
                backup_base=QUARK_BASE,
                timeout_seconds=5.0,
                sanitize_primary_dirs=False,
            )
        )

    async def test_verified_alias_is_first_and_repeated_play_does_not_repeat_failed_lookups(self):
        async def fs_get(path, **_kwargs):
            if path != VERIFIED_PATH:
                raise ArchiveUnavailableError("object not found")
            return {"raw_url": D2_URL}

        with patch.object(self.client, "_fs_get", side_effect=fs_get) as fetch:
            first = await self.client.resolve_stream_url(ORIGINAL_PATH, verified_paths=(VERIFIED_PATH,))
            second = await self.client.resolve_stream_url(ORIGINAL_PATH, verified_paths=(VERIFIED_PATH,))
        self.assertEqual((first, second), (D2_URL, D2_URL))
        self.assertEqual([call.args[0] for call in fetch.await_args_list], [VERIFIED_PATH])

    def test_ordinary_chinese_directory_can_use_verified_sec_directory(self):
        path = "八月/新文件名.mp4"
        self.assertEqual(
            self.client.remote_paths_for(path, sec_user_id="sec-1", verified_paths=(VERIFIED_PATH,)),
            [VERIFIED_PATH, f"{D2_BASE}/{path}", f"{QUARK_BASE}/{path}"],
        )

    def test_every_d2_candidate_precedes_a_verified_quark_alias(self):
        self.assertEqual(
            self.client.remote_paths_for(ORIGINAL_PATH, verified_paths=(VERIFIED_QUARK_PATH,)),
            [
                f"{D2_BASE}/{ORIGINAL_PATH}",
                f"{D2_BASE}/{SANITIZED_PATH}",
                f"{D2_BASE}/{DIRECTORY_ONLY_PATH}",
                VERIFIED_QUARK_PATH,
                f"{QUARK_BASE}/{SANITIZED_PATH}",
                f"{QUARK_BASE}/{DIRECTORY_ONLY_PATH}",
                f"{QUARK_BASE}/{ORIGINAL_PATH}",
            ],
        )

    def test_input_alias_order_cannot_put_quark_before_d2(self):
        candidates = self.client.remote_paths_for(
            ORIGINAL_PATH, verified_paths=(VERIFIED_QUARK_PATH, VERIFIED_PATH)
        )
        self.assertEqual(candidates[0], VERIFIED_PATH)
        self.assertEqual(candidates.index(VERIFIED_QUARK_PATH), 4)
        self.assertTrue(all(path.startswith(D2_BASE + "/") for path in candidates[:4]))

    def test_identical_verified_and_normal_paths_are_deduplicated(self):
        path = "八月/同名.mp4"
        remote = f"{D2_BASE}/{path}"
        self.assertEqual(self.client.remote_paths_for(path, verified_paths=(remote, remote)), [remote, f"{QUARK_BASE}/{path}"])

    async def test_failed_alias_is_explicit_when_no_exact_candidate_exists(self):
        with patch.object(self.client, "_fs_get", new=AsyncMock(side_effect=ArchiveUnavailableError("object not found"))) as fetch:
            with self.assertRaises(ArchiveUnavailableError) as raised:
                await self.client.resolve_stream_url(ORIGINAL_PATH, verified_paths=(VERIFIED_PATH,))
        self.assertIn(VERIFIED_PATH, str(raised.exception))
        self.assertIn("object not found", str(raised.exception))
        self.assertEqual(fetch.await_args_list[0].args, (VERIFIED_PATH,))
        self.assertEqual(len(fetch.await_args_list), 7)

    async def test_invalidation_clears_the_exact_alias_url_and_sign(self):
        with patch.object(self.client, "_fs_get", return_value={"sign": "sig:0"}) as fetch:
            first = await self.client.resolve_stream_url(ORIGINAL_PATH, verified_paths=(VERIFIED_PATH,))
            self.assertIn("%20%20", first)
            self.assertIn(VERIFIED_PATH, self.client._sign_cache)
            self.client.invalidate(ORIGINAL_PATH, verified_paths=(VERIFIED_PATH,))
            self.assertNotIn(VERIFIED_PATH, self.client._url_cache)
            self.assertNotIn(VERIFIED_PATH, self.client._sign_cache)
            await self.client.resolve_stream_url(ORIGINAL_PATH, verified_paths=(VERIFIED_PATH,))
        self.assertEqual(fetch.await_count, 2)

    def test_aliases_outside_configured_drives_fail_explicitly(self):
        with self.assertRaisesRegex(ArchiveUnavailableError, "不属于配置"):
            self.client.remote_paths_for(ORIGINAL_PATH, verified_paths=("/unrelated/file.mp4",))

    def test_no_verified_alias_does_not_guess_a_different_filename(self):
        path = "八月/unknown.mp4"
        self.assertEqual(
            self.client.remote_paths_for(path, sec_user_id="sec-1"),
            [f"{D2_BASE}/{path}", f"{QUARK_BASE}/{path}"],
        )


if __name__ == "__main__":
    unittest.main()
