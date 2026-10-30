from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from record_helpers import patch_monitor_users

from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.main import app
from douyin_user_monitor.monitor.openlist_client import ArchiveUnavailableError, OpenListClient
from douyin_user_monitor.settings import ArchiveSettings


def archive_user(**overrides: Any) -> Dict[str, Any]:
    user = {
        "id": "user-1",
        "sec_user_id": "sec-1",
        "nickname": "CutE1n",
        "avatar_url": "https://p3.example/a.jpg",
        "profile_url": "https://www.douyin.com/user/sec-1",
        "enabled": True,
        "downloaded_count": 2,
        "account_status": "normal",
        "account_status_label": "正常",
        "download_records": [
            {
                "aweme_id": "7001",
                "desc": "视频",
                "publish_time": "2026-08-29T15:00:36+08:00",
                "media_type": "video",
                "total_size_bytes": 100,
                "video_duration_ms": 11800,
                "files": ["CutE1n/20260829_150036_7001_aweme.mp4"],
            },
            {
                "aweme_id": "7002",
                "desc": "图文",
                "publish_time": "2026-05-20T19:52:45+08:00",
                "media_type": "image",
                "image_count": 2,
                "files": ["CutE1n/图片/标题/1.jpg", "CutE1n/图片/标题/2.jpg"],
            },
        ],
    }
    user.update(overrides)
    return user


class ArchiveListRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_lists_local_records_without_touching_douyin(self):
        with (
            patch_monitor_users([archive_user()]),
            patch.object(
                monitor_api.AWEME_FEED_SERVICE, "list_user_awemes", new=AsyncMock()
            ) as douyin_mock,
            patch.object(monitor_api.CRAWLER_CLIENT, "fetch_user_post_videos", new=AsyncMock()) as crawler_mock,
        ):
            response = self.client.get(
                "/api/monitor/users/user-1/archive", params={"page": 1, "page_size": 20}
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual([item["aweme_id"] for item in data["items"]], ["7001", "7002"])
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["total_pages"], 1)
        self.assertEqual(data["media_counts"], {"video": 1, "image": 1})
        self.assertEqual(data["user"]["nickname"], "CutE1n")
        # 关键：整条链路一次抖音请求都不发
        douyin_mock.assert_not_awaited()
        crawler_mock.assert_not_awaited()

    def test_works_for_user_whose_douyin_list_is_forbidden(self):
        """作品列表被隐藏(403)的账号，归档列表照样能看——这正是改用本地的原因。"""
        user = archive_user(last_error="获取用户作品列表失败: HTTP Status Code 403")
        with patch_monitor_users([user]):
            response = self.client.get("/api/monitor/users/user-1/archive")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 2)

    def test_unknown_user_is_404(self):
        with patch_monitor_users([]):
            response = self.client.get("/api/monitor/users/nope/archive")
        self.assertEqual(response.status_code, 404)

    def test_reports_archive_backend_state(self):
        with patch_monitor_users([archive_user()]):
            response = self.client.get("/api/monitor/users/user-1/archive")
        self.assertIn("archive_enabled", response.json()["data"])

    def test_prefetch_neighbors_queues_adjacent_works(self):
        with (
            patch_monitor_users([archive_user()]),
            patch.object(monitor_api, "_prefetch_archive_urls", new=AsyncMock()) as prefetch_mock,
        ):
            response = self.client.post("/api/monitor/users/user-1/archive/7001/prefetch")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["queued"], ["7002"])
        prefetch_mock.assert_awaited_once()
        user, items = prefetch_mock.await_args.args
        self.assertEqual(user["id"], "user-1")
        self.assertEqual([item["aweme_id"] for item in items], ["7002"])

    def test_playback_urls_returns_direct_links_and_separates_failures(self):
        user = archive_user()
        user["download_records"].append(
            {
                "aweme_id": "7003",
                "publish_time": "2026-04-01T10:00:00+08:00",
                "media_type": "video",
                "files": ["CutE1n/20260401_100000_7003_aweme.mp4"],
            }
        )
        user["download_records"].append(
            {
                "aweme_id": "7004",
                "publish_time": "2026-03-01T10:00:00+08:00",
                "media_type": "video",
                "files": ["CutE1n/20260301_100000_7004_aweme.mp4"],
            }
        )
        self_proxy = f"{monitor_api.OPENLIST_CLIENT._self_proxy_prefix()}/kuake/x.mp4?sign=s"  # noqa: SLF001

        async def fake_resolve(relative_path: str, **_: Any) -> str:
            if "7001" in relative_path:
                return "https://cdn.example/file/abc?auth_key=k"
            if "7003" in relative_path:
                return self_proxy
            if relative_path.endswith("/1.jpg"):
                return "https://cdn.example/file/img1"
            if relative_path.endswith("/2.jpg"):
                return self_proxy
            raise ArchiveUnavailableError("归档文件在主力盘与备份盘均不可用")

        with (
            patch_monitor_users([user]),
            patch.object(monitor_api.OPENLIST_CLIENT, "resolve_stream_url", new=AsyncMock(side_effect=fake_resolve)),
        ):
            response = self.client.post(
                "/api/monitor/users/user-1/archive/playback-urls",
                json={"aweme_ids": ["7001", "7002", "7003", "7004", "nope", "7001"]},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        # 7003 只能经本机 OpenList 自代理：None 让前端走代理
        self.assertEqual(data["urls"], {"7001": "https://cdn.example/file/abc?auth_key=k", "7003": None})
        # 图文按张给直链，自代理的那张同样为 None
        self.assertEqual(data["image_urls"], {"7002": ["https://cdn.example/file/img1", None]})
        self.assertEqual(set(data["errors"]), {"7004", "nope"})
        self.assertEqual(data["ttl_seconds"], monitor_api.ARCHIVE_DIRECT_URL_CLIENT_TTL_SECONDS)

    def test_playback_urls_rejects_empty_and_oversized_batches(self):
        with patch_monitor_users([archive_user()]):
            empty = self.client.post("/api/monitor/users/user-1/archive/playback-urls", json={"aweme_ids": []})
            huge = self.client.post(
                "/api/monitor/users/user-1/archive/playback-urls",
                json={"aweme_ids": [str(i) for i in range(101)]},
            )
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(huge.status_code, 422)

    def test_prefetch_neighbors_unknown_user_is_404(self):
        with patch_monitor_users([]):
            response = self.client.post("/api/monitor/users/nope/archive/7001/prefetch")
        self.assertEqual(response.status_code, 404)


class ArchiveMediaRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @staticmethod
    async def _body() -> Any:
        yield b"BYTES"

    def test_streams_from_openlist_with_range_passthrough(self):
        upstream = {
            "Content-Type": "video/mp4",
            "Content-Length": "5",
            "Content-Range": "bytes 0-4/58476442",
            "Accept-Ranges": "bytes",
        }
        opener = AsyncMock(return_value=(206, upstream, self._body()))
        with (
            patch_monitor_users([archive_user()]),
            patch.object(
                monitor_api.OPENLIST_CLIENT,
                "resolve_candidate_url",
                new=AsyncMock(return_value="http://127.0.0.1:5244/p/kuake/x.mp4?sign=s"),
            ) as resolve,
            patch.object(monitor_api, "open_media_stream", new=opener),
        ):
            response = self.client.get(
                "/api/monitor/users/user-1/archive/7001/media/0",
                headers={"Range": "bytes=0-4"},
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"BYTES")
        self.assertEqual(response.headers["content-range"], "bytes 0-4/58476442")
        self.assertEqual(opener.await_args.kwargs["headers"]["Range"], "bytes=0-4")
        # 相对路径来自记录，不接受调用方传路径
        resolve.assert_awaited_once_with(
            f"{monitor_api.SETTINGS.archive.primary_base}/CutE1n/20260829_150036_7001_aweme.mp4", refresh=False
        )

    def test_serves_image_by_file_index(self):
        opener = AsyncMock(return_value=(200, {"Content-Type": "image/jpeg"}, self._body()))
        with (
            patch_monitor_users([archive_user()]),
            patch.object(
                monitor_api.OPENLIST_CLIENT, "resolve_candidate_url", new=AsyncMock(return_value="http://x/p/a.jpg")
            ) as resolve,
            patch.object(monitor_api, "open_media_stream", new=opener),
        ):
            response = self.client.get("/api/monitor/users/user-1/archive/7002/media/1")

        self.assertEqual(response.status_code, 200)
        resolve.assert_awaited_once_with(
            f"{monitor_api.SETTINGS.archive.primary_base}/CutE1n/图片/标题/2.jpg", refresh=False
        )

    def test_uses_cloud_even_when_local_file_still_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_file = root / "CutE1n" / "20260829_150036_7001_aweme.mp4"
            local_file.parent.mkdir(parents=True)
            local_file.write_bytes(b"LOCALDATA")
            patched_settings = replace(
                monitor_api.SETTINGS,
                monitor=replace(monitor_api.SETTINGS.monitor, download_root=root),
            )
            with (
                patch_monitor_users([archive_user()]),
                patch.object(monitor_api, "SETTINGS", patched_settings),
                patch.object(
                    monitor_api.OPENLIST_CLIENT, "resolve_candidate_url",
                    new=AsyncMock(return_value="https://archive.test/file.mp4"),
                ) as resolve,
                patch.object(monitor_api, "open_media_stream", new=AsyncMock(return_value=(
                    200, {"Content-Type": "video/mp4"}, self._body(),
                ))),
            ):
                response = self.client.get("/api/monitor/users/user-1/archive/7001/media/0")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"BYTES")
            self.assertEqual(local_file.read_bytes(), b"LOCALDATA")
            resolve.assert_awaited_once()

    def test_unknown_aweme_is_404(self):
        with patch_monitor_users([archive_user()]):
            response = self.client.get("/api/monitor/users/user-1/archive/missing/media/0")
        self.assertEqual(response.status_code, 404)

    def test_out_of_range_index_is_404(self):
        with patch_monitor_users([archive_user()]):
            response = self.client.get("/api/monitor/users/user-1/archive/7001/media/9")
        self.assertEqual(response.status_code, 404)

    def test_archive_unavailable_is_404(self):
        with (
            patch_monitor_users([archive_user()]),
            patch.object(
                monitor_api.OPENLIST_CLIENT,
                "resolve_candidate_url",
                new=AsyncMock(side_effect=ArchiveUnavailableError("归档文件不可用: object not found")),
            ),
        ):
            response = self.client.get("/api/monitor/users/user-1/archive/7001/media/0")
        self.assertEqual(response.status_code, 404)

    def test_upstream_failure_is_502(self):
        with (
            patch_monitor_users([archive_user()]),
            patch.object(
                monitor_api.OPENLIST_CLIENT, "resolve_candidate_url", new=AsyncMock(return_value="http://x/p/a.mp4")
            ),
            patch.object(
                monitor_api,
                "open_media_stream",
                new=AsyncMock(side_effect=monitor_api.MediaUpstreamError("媒体拉取失败: status=500")),
            ),
        ):
            response = self.client.get("/api/monitor/users/user-1/archive/7001/media/0")
        self.assertEqual(response.status_code, 502)


def archive_settings(
    tmp_dir: Path,
    *,
    enabled: bool = True,
    token: str = "",
    primary_base: str = "/wopan/抖音视频",
    backup_base: str = "/kuake/抖音视频",
    sanitize_primary_dirs: bool = True,
) -> ArchiveSettings:
    return ArchiveSettings(
        enabled=enabled,
        openlist_base_url="http://127.0.0.1:5244",
        openlist_db_path=tmp_dir / "data.db",
        openlist_token=token,
        primary_base=primary_base,
        backup_base=backup_base,
        timeout_seconds=5.0,
        sanitize_primary_dirs=sanitize_primary_dirs,
    )


class FakeFsGetResponse:
    """最小化的 httpx 响应替身。"""

    status_code = 200

    def __init__(self, payload: Dict[str, Any]):
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


def ok(**data: Any) -> FakeFsGetResponse:
    return FakeFsGetResponse({"code": 200, "data": data})


def err(message: str) -> FakeFsGetResponse:
    return FakeFsGetResponse({"code": 500, "message": message})


class OpenListClientTests(unittest.IsolatedAsyncioTestCase):
    def test_remote_path_joins_primary_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp)))
            self.assertEqual(
                client.remote_path_for("CutE1n/a.mp4"),
                "/wopan/抖音视频/CutE1n/a.mp4",
            )
            with self.assertRaises(ArchiveUnavailableError):
                client.remote_path_for("  ")

    def test_read_candidates_are_primary_then_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp)))
            self.assertEqual(
                client.remote_paths_for("CutE1n/a.mp4"),
                ["/wopan/抖音视频/CutE1n/a.mp4", "/kuake/抖音视频/CutE1n/a.mp4"],
            )

    def test_primary_and_backup_both_sanitise_dir_with_original_fallback(self):
        """三候选：主力+备份都查去 emoji 形态，备份盘再留原名兜底（rename 前/空白差异）。"""
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp)))
            self.assertEqual(
                client.remote_paths_for("鱼幼薇🐱/a.mp4"),
                [
                    "/wopan/抖音视频/鱼幼薇/a.mp4",
                    "/kuake/抖音视频/鱼幼薇/a.mp4",
                    "/kuake/抖音视频/鱼幼薇🐱/a.mp4",
                ],
            )

    def test_primary_prefers_directory_only_then_legacy_filename_sanitising(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp)))
            self.assertEqual(client.remote_paths_for("用户/带🍬表情的文件名.mp4")[:2], [
                "/wopan/抖音视频/用户/带🍬表情的文件名.mp4",
                "/wopan/抖音视频/用户/带表情的文件名.mp4",
            ])

    def test_nested_image_dirs_are_all_sanitised(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp)))
            self.assertEqual(
                client.remote_paths_for("柒柒🐿️/图片/标题🍬/1.jpg")[0],
                "/wopan/抖音视频/柒柒/图片/标题/1.jpg",
            )

    def test_all_emoji_dir_falls_back_to_backup_only(self):
        """目录名整体是 emoji 且未提供 sec_user_id，主力盘无法落地，只能走备份盘原名。"""
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp)))
            self.assertEqual(
                client.remote_paths_for("🐱🍬/a.mp4"),
                ["/kuake/抖音视频/🐱🍬/a.mp4"],
            )

    def test_all_emoji_dir_uses_sec_uid_fallback_when_provided(self):
        """全 emoji 目录有 sec_user_id 时，主力+备份都查 sec_ 兜底目录，备份再留原名兜底。"""
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp)))
            self.assertEqual(
                client.remote_paths_for("🐱🍬/a.mp4", sec_user_id="sec-1"),
                [
                    "/wopan/抖音视频/sec_sec-1/a.mp4",
                    "/kuake/抖音视频/sec_sec-1/a.mp4",
                    "/kuake/抖音视频/🐱🍬/a.mp4",
                ],
            )

    def test_sanitising_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp), sanitize_primary_dirs=False))
            self.assertEqual(
                client.remote_paths_for("鱼幼薇🐱/a.mp4")[0],
                "/wopan/抖音视频/鱼幼薇🐱/a.mp4",
            )

    def test_identical_bases_are_deduped(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(
                archive_settings(Path(tmp), primary_base="/kuake/x", backup_base="/kuake/x")
            )
            self.assertEqual(client.remote_paths_for("a.mp4"), ["/kuake/x/a.mp4"])

    def test_stream_url_encodes_path_but_keeps_slashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp)))
            url = client.build_stream_url("/kuake/抖音视频/CutE1n/a b.mp4", "sig+n=:0")
            self.assertTrue(url.startswith("http://127.0.0.1:5244/p/kuake/"))
            self.assertIn("/CutE1n/", url)
            self.assertNotIn(" ", url)
            self.assertIn("sign=sig%2Bn%3D%3A0", url)

    def test_reads_token_from_openlist_sqlite_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE x_setting_items (key TEXT, value TEXT)")
            connection.execute("INSERT INTO x_setting_items VALUES ('token', 'tok-123')")
            connection.commit()
            connection.close()
            client = OpenListClient(archive_settings(Path(tmp)))
            self.assertEqual(client._read_token_from_db(), "tok-123")  # noqa: SLF001

    def test_missing_db_raises_archive_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp)))
            with self.assertRaises(ArchiveUnavailableError):
                client._read_token_from_db()  # noqa: SLF001

    async def test_disabled_backend_refuses_to_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp), enabled=False))
            self.assertFalse(client.enabled)
            with self.assertRaises(ArchiveUnavailableError):
                await client.resolve_sign("/kuake/抖音视频/a.mp4")

    async def test_sign_is_cached_per_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp), token="tok"))
            post = AsyncMock(return_value=ok(sign="abc=:0"))
            with patch("httpx.AsyncClient.post", new=post):
                first = await client.resolve_sign("/kuake/抖音视频/a.mp4")
                second = await client.resolve_sign("/kuake/抖音视频/a.mp4")
            self.assertEqual(first, "abc=:0")
            self.assertEqual(second, "abc=:0")
            self.assertEqual(post.await_count, 1)

    async def test_openlist_error_payload_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp), token="tok"))
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=err("object not found"))):
                with self.assertRaises(ArchiveUnavailableError) as ctx:
                    await client.resolve_sign("/kuake/抖音视频/missing.mp4")
            self.assertIn("object not found", str(ctx.exception))

    async def test_uses_upstream_raw_url_when_storage_is_not_proxied(self):
        """wopan / d2 的 web_proxy 是关的：必须用 raw_url，打 /p 会 403。"""
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp), token="tok"))
            raw = "https://wopan-cdn.example/file/abc?token=xyz"
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=ok(raw_url=raw, sign="s=:0"))):
                url = await client.resolve_stream_url("CutE1n/a.mp4")
            self.assertEqual(url, raw)

    async def test_uses_openlist_proxy_when_raw_url_points_back_at_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp), token="tok"))
            self_proxy = "http://127.0.0.1:5244/p/kuake/x.mp4?sign=s=:0"
            with patch(
                "httpx.AsyncClient.post", new=AsyncMock(return_value=ok(raw_url=self_proxy, sign="s=:0"))
            ):
                url = await client.resolve_stream_url("CutE1n/a.mp4")
            self.assertTrue(url.startswith("http://127.0.0.1:5244/p/"))
            self.assertIn("sign=", url)

    async def test_falls_back_to_backup_when_missing_on_primary(self):
        """历史作品只在备份盘上，回落是它们能播放的唯一途径。"""
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp), token="tok"))
            calls = []

            async def fake_post(self, url, **kwargs):  # noqa: ANN001
                path = kwargs["json"]["path"]
                calls.append(path)
                if path.startswith("/wopan/"):
                    return err("object not found")
                return ok(raw_url="https://kuake-cdn.example/f")

            with patch("httpx.AsyncClient.post", new=fake_post):
                url = await client.resolve_stream_url("CutE1n/a.mp4")

            self.assertEqual(url, "https://kuake-cdn.example/f")
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[0].startswith("/wopan/"))
            self.assertTrue(calls[1].startswith("/kuake/"))

    async def test_raises_when_missing_on_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenListClient(archive_settings(Path(tmp), token="tok"))
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=err("object not found"))):
                with self.assertRaises(ArchiveUnavailableError) as ctx:
                    await client.resolve_stream_url("CutE1n/a.mp4")
            message = str(ctx.exception)
            self.assertIn("主力盘与备份盘均不可用", message)
            self.assertIn("/wopan/", message)
            self.assertIn("/kuake/", message)

    async def test_raw_url_cache_expires_and_invalidate_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = {"t": 0.0}
            client = OpenListClient(
                archive_settings(Path(tmp), token="tok"), clock=lambda: now["t"]
            )
            post = AsyncMock(return_value=ok(raw_url="https://wopan-cdn.example/f"))
            with patch("httpx.AsyncClient.post", new=post):
                await client.resolve_stream_url("a.mp4")
                await client.resolve_stream_url("a.mp4")
                self.assertEqual(post.await_count, 1)  # 命中缓存
                now["t"] = 301.0                       # 直链短缓存过期
                await client.resolve_stream_url("a.mp4")
                self.assertEqual(post.await_count, 2)
                client.invalidate("a.mp4")             # 主动失效
                await client.resolve_stream_url("a.mp4")
                self.assertEqual(post.await_count, 3)


if __name__ == "__main__":
    unittest.main()
