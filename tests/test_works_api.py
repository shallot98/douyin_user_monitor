from __future__ import annotations

import unittest
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.main import app
from record_helpers import patch_monitor_users


def sample_user(**overrides: Any) -> Dict[str, Any]:
    user = {
        "id": "user-1",
        "sec_user_id": "sec-1",
        "nickname": "阿花",
        "avatar_url": "https://p3.example/avatar.jpg",
        "profile_url": "https://www.douyin.com/user/sec-1",
        "enabled": True,
        "downloaded_count": 12,
        "account_status": "normal",
        "account_status_label": "正常",
        "last_checked_at": "2026-08-30T10:00:00+08:00",
        "downloaded_aweme_ids": ["7001"],
        # 大字段：确认不会被带进作品页响应
        "download_records": [{"aweme_id": "7001", "files": ["阿花/a.mp4"]}],
    }
    user.update(overrides)
    return user


FEED_PAGE = {
    "items": [
        {"aweme_id": "7001", "media_type": "video", "downloaded": True},
        {"aweme_id": "7002", "media_type": "image", "downloaded": False},
    ],
    "has_more": True,
    "next_cursor": 1787611006000,
}


class ListUserAwemesRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_returns_items_plus_slim_user_header(self):
        users = [sample_user()]
        with (
            patch_monitor_users(users),
            patch.object(
                monitor_api.AWEME_FEED_SERVICE,
                "list_user_awemes",
                new=AsyncMock(return_value=dict(FEED_PAGE)),
            ) as feed_mock,
        ):
            response = self.client.get("/api/monitor/users/user-1/awemes", params={"cursor": 5, "count": 3})

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual([item["aweme_id"] for item in data["items"]], ["7001", "7002"])
        self.assertTrue(data["has_more"])
        self.assertEqual(data["next_cursor"], 1787611006000)

        header = data["user"]
        self.assertEqual(header["nickname"], "阿花")
        self.assertEqual(header["downloaded_count"], 12)
        # 头部只带展示所需字段，不外泄大字段
        self.assertNotIn("download_records", header)
        self.assertNotIn("downloaded_aweme_ids", header)
        self.assertNotIn("sec_user_id", header)

        feed_mock.assert_awaited_once()
        kwargs = feed_mock.await_args.kwargs
        self.assertEqual(feed_mock.await_args.args[0], "sec-1")
        self.assertEqual(kwargs["cursor"], 5)
        self.assertEqual(kwargs["count"], 3)
        self.assertEqual(list(kwargs["downloaded_ids"]), ["7001"])

    def test_header_exposes_former_nicknames_for_renamed_user(self):
        # 归档目录以下载当时的昵称命名，改名后档案分散；头部带上曾用名以便对应网盘目录
        users = [
            sample_user(
                nickname="新名",
                nickname_history=["很久以前"],
                download_records=[
                    {"aweme_id": "7001", "files": ["新名/a.mp4"]},
                    {"aweme_id": "7000", "files": ["旧名/图片/标题/1.jpg"]},
                ],
            )
        ]
        with (
            patch_monitor_users(users),
            patch.object(
                monitor_api.AWEME_FEED_SERVICE,
                "list_user_awemes",
                new=AsyncMock(return_value=dict(FEED_PAGE)),
            ),
        ):
            response = self.client.get("/api/monitor/users/user-1/awemes")

        header = response.json()["data"]["user"]
        self.assertEqual(header["former_nicknames"], ["很久以前", "旧名"])

    def test_header_former_nicknames_empty_without_rename(self):
        with (
            patch_monitor_users([sample_user()]),
            patch.object(
                monitor_api.AWEME_FEED_SERVICE,
                "list_user_awemes",
                new=AsyncMock(return_value=dict(FEED_PAGE)),
            ),
        ):
            response = self.client.get("/api/monitor/users/user-1/awemes")
        self.assertEqual(response.json()["data"]["user"]["former_nicknames"], [])

    def test_unknown_user_is_404(self):
        with patch_monitor_users([]):
            response = self.client.get("/api/monitor/users/nope/awemes")
        self.assertEqual(response.status_code, 404)

    def test_user_without_sec_user_id_is_rejected(self):
        users = [sample_user(sec_user_id="")]
        with patch_monitor_users(users):
            response = self.client.get("/api/monitor/users/user-1/awemes")
        self.assertEqual(response.status_code, 400)

    def test_upstream_failure_maps_to_502(self):
        users = [sample_user()]
        with (
            patch_monitor_users(users),
            patch.object(
                monitor_api.AWEME_FEED_SERVICE,
                "list_user_awemes",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            response = self.client.get("/api/monitor/users/user-1/awemes")
        self.assertEqual(response.status_code, 502)


class ProxyAwemeVideoRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def _patched(self, *, stream_result=None, stream_error=None, resolve_error=None):
        users = [sample_user()]
        resolve = (
            AsyncMock(side_effect=resolve_error)
            if resolve_error is not None
            else AsyncMock(return_value="https://v5.example/play.mp4")
        )
        opener = (
            AsyncMock(side_effect=stream_error)
            if stream_error is not None
            else AsyncMock(return_value=stream_result)
        )
        return (
            patch_monitor_users(users),
            patch.object(monitor_api.AWEME_FEED_SERVICE, "resolve_play_url", new=resolve),
            patch.object(
                monitor_api.CRAWLER_CLIENT,
                "get_douyin_headers",
                new=AsyncMock(return_value={"headers": {"User-Agent": "ua", "Referer": "https://www.douyin.com/"}}),
            ),
            patch.object(monitor_api, "open_media_stream", new=opener),
            opener,
            resolve,
        )

    @staticmethod
    async def _body() -> Any:
        yield b"abcd"

    def test_streams_partial_content_with_range_passthrough(self):
        upstream_headers = {
            "Content-Type": "video/mp4",
            "Content-Length": "4",
            "Content-Range": "bytes 0-3/1000",
            "Accept-Ranges": "bytes",
        }
        p1, p2, p3, p4, opener, _ = self._patched(
            stream_result=(206, upstream_headers, self._body())
        )
        with p1, p2, p3, p4:
            response = self.client.get(
                "/api/monitor/users/user-1/awemes/7001/video",
                headers={"Range": "bytes=0-3"},
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"abcd")
        self.assertEqual(response.headers["content-range"], "bytes 0-3/1000")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        # 客户端 Range 必须转给上游，否则拖进度条会从头下载
        upstream_sent = opener.await_args.kwargs["headers"]
        self.assertEqual(upstream_sent["Range"], "bytes=0-3")
        self.assertEqual(upstream_sent["Referer"], "https://www.douyin.com/")
        self.assertEqual(opener.await_args.args[0], "https://v5.example/play.mp4")

    def test_range_error_is_not_cached_as_playable_media(self):
        p1, p2, p3, p4, _, _ = self._patched(
            stream_result=(416, {"Content-Range": "bytes */1000"}, self._body())
        )
        with p1, p2, p3, p4:
            response = self.client.get(
                "/api/monitor/users/user-1/awemes/7001/video",
                headers={"Range": "bytes=2000-"},
            )
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], "bytes */1000")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_full_body_without_range(self):
        p1, p2, p3, p4, _, _ = self._patched(
            stream_result=(200, {"Content-Type": "video/mp4"}, self._body())
        )
        with p1, p2, p3, p4:
            response = self.client.get("/api/monitor/users/user-1/awemes/7001/video")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"abcd")

    def test_unresolvable_aweme_maps_to_502(self):
        p1, p2, p3, p4, _, _ = self._patched(resolve_error=ValueError("作品 7001 未找到可播放地址"))
        with p1, p2, p3, p4:
            response = self.client.get("/api/monitor/users/user-1/awemes/7001/video")
        self.assertEqual(response.status_code, 502)

    def test_upstream_reject_maps_to_502(self):
        p1, p2, p3, p4, _, _ = self._patched(
            stream_error=monitor_api.MediaUpstreamError("媒体拉取失败: status=403", status_code=403)
        )
        with p1, p2, p3, p4:
            response = self.client.get("/api/monitor/users/user-1/awemes/7001/video")
        self.assertEqual(response.status_code, 502)

    def test_unknown_user_is_404_before_any_upstream_call(self):
        with patch_monitor_users([]):
            with patch.object(
                monitor_api.AWEME_FEED_SERVICE, "resolve_play_url", new=AsyncMock()
            ) as resolve:
                response = self.client.get("/api/monitor/users/nope/awemes/7001/video")
        self.assertEqual(response.status_code, 404)
        resolve.assert_not_awaited()


class WorksPageTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_page_served_with_no_cache(self):
        response = self.client.get("/api/monitor/works/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers.get("cache-control", ""))

    def test_page_reads_local_archive_not_douyin(self):
        html = self.client.get("/api/monitor/works/dashboard").text
        # 列表只走本地归档端点；实时抖音端点不应出现在页面里
        self.assertIn("/archive?", html)
        # 媒体地址拼接已抽到共享播放器模块
        self.assertIn('src="/static/works-viewer.js', html)
        viewer_js = self.client.get("/static/works-viewer.js").text
        self.assertIn("/archive`", viewer_js)
        self.assertIn("${encodeURIComponent(item.aweme_id)}/media/", viewer_js)
        self.assertNotIn("/awemes", viewer_js)
        self.assertNotIn("/awemes?", html)
        self.assertNotIn("/awemes/", html)
        self.assertIn('src="/static/common.js', html)
        self.assertIn("user_id", html)

    def test_video_cards_use_placeholder_since_archive_has_no_cover(self):
        html = self.client.get("/api/monitor/works/dashboard").text
        self.assertIn("work-cover-empty", html)
        # 封面地址回退到归档封面文件的逻辑在共享播放器模块里
        viewer_js = self.client.get("/static/works-viewer.js").text
        self.assertIn("item.cover_index", viewer_js)

    def test_page_escapes_untrusted_desc_and_restricts_media_urls(self):
        html = self.client.get("/api/monitor/works/dashboard").text
        self.assertIn("function escapeHtml(", html)
        self.assertIn("function safeMediaUrl(", html)
        self.assertIn("escapeHtml(desc)", html)
        # 缩略图/大图直连 CDN 时必须去掉 Referer，否则抖音 CDN 会 403
        self.assertIn('referrerpolicy="no-referrer"', html)

    def test_statistics_avatar_links_to_works_page(self):
        html = self.client.get("/api/monitor/statistics/dashboard").text
        self.assertIn("/api/monitor/works/dashboard?user_id=", html)
        self.assertIn("user-avatar-link", html)
        self.assertIn("查看作品列表", html)

    def test_page_renders_former_nicknames(self):
        html = self.client.get("/api/monitor/works/dashboard").text
        self.assertIn("user.former_nicknames", html)
        self.assertIn("曾用名 / 归档目录", html)
        self.assertIn("alias-chip", html)
        # 曾用名来自抖音端，属不可信输入
        self.assertIn("escapeHtml(name)", html)

    def test_works_list_is_paginated_not_infinite_append(self):
        html = self.client.get("/api/monitor/works/dashboard").text
        # 「加载更多」式追加已换成分页条
        self.assertNotIn("loadMoreBtn", html)
        self.assertNotIn("加载更多", html)
        self.assertIn('id="worksPagination"', html)
        self.assertIn("pagination-controls", html)
        self.assertIn("function renderPagination(", html)
        self.assertIn("function renderPageNumbers(", html)
        self.assertIn("上一页", html)
        self.assertIn("下一页", html)

    def test_pagination_is_offset_based_with_jumpable_pages(self):
        html = self.client.get("/api/monitor/works/dashboard").text
        # 本地列表总数已知，所以有末页按钮、可任意跳页（游标翻页做不到）
        self.assertIn('id="worksLastBtn"', html)
        self.assertIn("goToPage(state.totalPages)", html)
        self.assertIn("state.totalPages", html)
        self.assertIn('id="worksPageSize"', html)
        self.assertIn("function onPageSizeChange(", html)

    def test_page_supports_embedded_mode(self):
        html = self.client.get("/api/monitor/works/dashboard").text
        self.assertIn('PAGE_QUERY.get("embed") === "1"', html)
        self.assertIn('document.body.classList.add("embed")', html)
        # 嵌入时外层浮层已有标题栏/关闭按钮，页内这些要藏掉
        self.assertIn("body.embed .sidebar", html)
        self.assertIn("body.embed .top-header", html)
        # 焦点在 iframe 内时父页收不到按键，Esc 必须由内向外传
        self.assertIn('window.parent.postMessage({ type: "works:close" }', html)


class WorksOverlayTests(unittest.TestCase):
    """点头像不再跳页：浮层打开，返回时表格状态与滚动位置原样保留。"""

    def setUp(self):
        self.client = TestClient(app)
        self.html = self.client.get("/api/monitor/statistics/dashboard").text

    def test_avatar_click_opens_overlay_instead_of_navigating(self):
        self.assertIn("onAvatarClick(event,", self.html)
        self.assertIn("event.preventDefault();", self.html)
        self.assertIn("function openWorksOverlay(", self.html)
        self.assertIn("works-overlay-frame", self.html)
        self.assertIn("embed=1", self.html)

    def test_avatar_keeps_href_for_new_tab(self):
        # 带修饰键/中键时仍要走浏览器原生新标签页行为
        self.assertIn('href="${worksPageUrl(userId)}"', self.html)
        self.assertIn("event.metaKey || event.ctrlKey", self.html)

    def test_back_button_closes_overlay_rather_than_leaving_page(self):
        self.assertIn("history.pushState({ worksOverlay: id }", self.html)
        self.assertIn('window.addEventListener("popstate"', self.html)
        self.assertIn("fromHistory: true", self.html)

    def test_close_stops_iframe_video_stream(self):
        self.assertIn('worksFrame").src = "about:blank"', self.html)

    def test_child_close_messages_are_origin_checked(self):
        self.assertIn("event.origin !== window.location.origin", self.html)
        self.assertIn('event.data.type === "works:close"', self.html)

    def test_auto_refresh_paused_while_overlay_open(self):
        self.assertIn("if (!worksOverlayState.open) refreshAll();", self.html)


if __name__ == "__main__":
    unittest.main()
