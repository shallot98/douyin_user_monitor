from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from douyin_user_monitor.monitor.openlist_client import ArchiveUnavailableError, OpenListClient
from douyin_user_monitor.settings import ArchiveSettings

D2_BASE = "/d2/vps/抖音视频"
QUARK_BASE = "/kuake/抖音视频"
ORIGINAL_PATH = "小妮💃/作品🍬.mp4"
LEGACY_PATH = "小妮/作品.mp4"
DIRECTORY_ONLY_PATH = "小妮/作品🍬.mp4"
D2_URL = "https://d2.example.test/video.mp4"
QUARK_URL = "https://quark.example.test/video.mp4"


class D2PlaybackTests(unittest.IsolatedAsyncioTestCase):
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

    async def _resolve_from(self, relative_path, available):
        async def fs_get(remote_path, **_kwargs):
            if remote_path not in available:
                raise ArchiveUnavailableError("object not found")
            return {"raw_url": available[remote_path]}

        with patch.object(self.client, "_fs_get", side_effect=fs_get) as fetch:
            url = await self.client.resolve_stream_url(relative_path)
        return url, [call.args[0] for call in fetch.await_args_list]

    async def test_prefers_original_d2_over_legacy_and_backup_copies(self):
        original = f"{D2_BASE}/{ORIGINAL_PATH}"
        url, paths = await self._resolve_from(
            ORIGINAL_PATH,
            {
                original: D2_URL,
                f"{D2_BASE}/{LEGACY_PATH}": "https://d2.example.test/legacy.mp4",
                f"{QUARK_BASE}/{LEGACY_PATH}": QUARK_URL,
            },
        )
        self.assertEqual(url, D2_URL)
        self.assertEqual(paths, [original])

    async def test_uses_legacy_d2_copy_before_quark(self):
        legacy = f"{D2_BASE}/{LEGACY_PATH}"
        url, paths = await self._resolve_from(
            ORIGINAL_PATH,
            {legacy: D2_URL, f"{QUARK_BASE}/{ORIGINAL_PATH}": QUARK_URL},
        )
        self.assertEqual(url, D2_URL)
        self.assertEqual(paths, [f"{D2_BASE}/{ORIGINAL_PATH}", legacy])

    async def test_finds_legacy_quark_copy_when_d2_has_no_copy(self):
        legacy = f"{QUARK_BASE}/{LEGACY_PATH}"
        url, paths = await self._resolve_from(ORIGINAL_PATH, {legacy: QUARK_URL})
        self.assertEqual(url, QUARK_URL)
        self.assertEqual(
            paths, [f"{D2_BASE}/{ORIGINAL_PATH}", f"{D2_BASE}/{LEGACY_PATH}", f"{D2_BASE}/{DIRECTORY_ONLY_PATH}", legacy]
        )

    async def test_finds_original_quark_copy_after_legacy_candidates(self):
        original = f"{QUARK_BASE}/{ORIGINAL_PATH}"
        url, paths = await self._resolve_from(ORIGINAL_PATH, {original: QUARK_URL})
        self.assertEqual(url, QUARK_URL)
        self.assertEqual(
            paths,
            [
                f"{D2_BASE}/{ORIGINAL_PATH}",
                f"{D2_BASE}/{LEGACY_PATH}",
                f"{D2_BASE}/{DIRECTORY_ONLY_PATH}",
                f"{QUARK_BASE}/{LEGACY_PATH}",
                f"{QUARK_BASE}/{DIRECTORY_ONLY_PATH}",
                original,
            ],
        )

    async def test_all_emoji_original_d2_directory_needs_no_sec_user_id(self):
        relative = "🐱🍬/a.mp4"
        original = f"{D2_BASE}/{relative}"
        url, paths = await self._resolve_from(relative, {original: D2_URL})
        self.assertEqual(url, D2_URL)
        self.assertEqual(paths, [original])
        self.assertEqual(
            self.client.remote_paths_for(relative), [original, f"{QUARK_BASE}/{relative}"]
        )

    async def test_unchanged_names_do_not_duplicate_lookups(self):
        relative = "CutE1n/a.mp4"
        backup = f"{QUARK_BASE}/{relative}"
        url, paths = await self._resolve_from(relative, {backup: QUARK_URL})
        self.assertEqual(url, QUARK_URL)
        self.assertEqual(paths, [f"{D2_BASE}/{relative}", backup])


if __name__ == "__main__":
    unittest.main()
